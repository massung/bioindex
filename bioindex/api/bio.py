import concurrent.futures
import fastapi
import itertools

from pydantic import BaseModel
from typing import List, Optional

from ..lib import aws
from ..lib import config
from ..lib import continuation
from ..lib import index
from ..lib import query

from ..lib.auth import restricted_keywords
from ..lib.utils import nonce, profile

# load dot files and configuration
CONFIG = config.Config()

# create flask app; this will load .env
router = fastapi.APIRouter()

# connect to database
engine = aws.connect_to_rds(CONFIG.rds_instance, schema=CONFIG.bio_schema)
portal = aws.connect_to_rds(CONFIG.rds_instance, schema=CONFIG.portal_schema)

# max number of bytes to read from s3 per request
RESPONSE_LIMIT = CONFIG.response_limit
MATCH_LIMIT = CONFIG.match_limit

# multi-query executor
executor = concurrent.futures.ThreadPoolExecutor(max_workers=20)


class Query(BaseModel):
    q: List[str]
    fmt: Optional[str] = 'row'
    limit: Optional[int] = None



def _load_indexes():
    """
    Create a cache of the indexes in the database.
    """
    indexes = index.Index.list_indexes(engine, filter_built=False)
    return dict((i.name, i) for i in indexes)


# initialize with all the indexes, get them all, whether built or not
INDEXES = _load_indexes()


@router.get('/indexes', response_class=fastapi.responses.ORJSONResponse)
async def api_list_indexes():
    """
    Return all queryable indexes. This also refreshes the internal
    cache of the table so the server doesn't need to be bounced when
    the table is updated (very rare!).
    """
    global INDEXES

    # update the global index cache
    INDEXES = _load_indexes()
    data = []

    # add each index to the response data
    for i in sorted(INDEXES.values(), key=lambda i: i.name):
        data.append({
            'index': i.name,
            'built': i.built,
            'schema': str(i.schema),
            'query': {
                'keys': i.schema.key_columns,
                'locus': i.schema.has_locus,
            },
        })

    return {
        'count': len(data),
        'data': data,
        'nonce': nonce(),
    }


@router.get('/match/{index}', response_class=fastapi.responses.ORJSONResponse)
async def api_match(index: str, req: fastapi.Request, q: str, limit: int = None):
    """
    Return all the unique keys for a value-indexed table.
    """
    try:
        i = INDEXES[index]
        qs = _parse_query(q)

        # execute the query
        keys, query_s = profile(query.match, engine, i, qs)

        # allow an upper limit on the total number of keys returned
        if limit is not None:
            keys = itertools.islice(keys, limit)

        # read the matched keys
        return _match_keys(keys, index, qs, limit, query_s=query_s)
    except KeyError:
        raise fastapi.HTTPException(
            status_code=400, detail=f'Invalid index: {index}')
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e))


@router.get('/count/{index}', response_class=fastapi.responses.ORJSONResponse)
async def api_count_index(index: str, req: fastapi.Request, q: str=None):
    """
    Query the database and estimate how many records will be returned.
    """
    try:
        i = INDEXES[index]
        qs = _parse_query(q)

        # lookup the schema for this index and perform the query
        count, query_s = profile(query.count, engine, CONFIG.s3_bucket, i, qs)

        return {
            'profile': {
                'query': query_s,
            },
            'index': index,
            'q': qs,
            'count': count,
            'nonce': nonce(),
        }
    except KeyError:
        raise fastapi.HTTPException(
            status_code=400, detail=f'Invalid index: {index}')
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e))


@router.get('/all/{index}', response_class=fastapi.responses.ORJSONResponse)
async def api_all(index: str, req: fastapi.Request, fmt: str='row'):
    """
    Query the database and return ALL records for a given index.
    """
    try:
        i = INDEXES[index]

        # discover what the user doesn't have access to see
        restricted, auth_s = profile(restricted_keywords, portal, req)

        # lookup the schema for this index and perform the query
        reader, query_s = profile(
            query.fetch_all,
            CONFIG.s3_bucket,
            i.s3_prefix,
            restricted=restricted,
        )

        # fetch records from the reader
        return _fetch_records(
            reader, index, None, fmt, query_s=auth_s + query_s)
    except KeyError:
        raise fastapi.HTTPException(
            status_code=400, detail=f'Invalid index: {index}')
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e))


@router.head('/all/{index}', response_class=fastapi.responses.ORJSONResponse)
async def api_test_all(index: str, req: fastapi.Request):
    """
    Query the database fetch ALL records for a given index. Don't read
    the records from S3, but instead set the Content-Length to the total
    number of bytes what would be read.
    """
    try:
        i = INDEXES[index]

        # lookup the schema for this index and perform the query
        reader, query_s = profile(
            query.fetch_all,
            CONFIG.s3_bucket,
            i.s3_prefix,
        )

        # return the total number of bytes that need to be read
        return fastapi.Response(
            headers={'Content-Length': str(reader.bytes_total)})
    except KeyError:
        raise fastapi.HTTPException(
            status_code=400, detail=f'Invalid index: {index}')
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e))


@router.get('/query/{index}', response_class=fastapi.responses.ORJSONResponse)
async def api_query_index(index: str, q: str, req: fastapi.Request, fmt='row', limit: int=None):
    """
    Query the database for records matching the query parameter and
    read the records from s3.
    """
    try:
        i = INDEXES[index]
        qs = _parse_query(q, required=True)

        # discover what the user doesn't have access to see
        restricted, auth_s = profile(restricted_keywords, portal, req)

        # lookup the schema for this index and perform the query
        reader, query_s = profile(
            query.fetch,
            engine,
            CONFIG.s3_bucket,
            i,
            qs,
            restricted=restricted,
        )

        # use a zip to limit the total number of records that will be read
        if limit is not None:
            reader.set_limit(limit)

        # the results of the query
        return _fetch_records(reader, index, qs, fmt, query_s=auth_s + query_s)
    except KeyError:
        raise fastapi.HTTPException(
            status_code=400, detail=f'Invalid index: {index}')
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e))


#@router.post('/query/{index}', response_class=fastapi.responses.ORJSONResponse)
async def api_query_index_multi(index: str, qs: Query, req: fastapi.Request):
    """
    Issue multiple queries in parallel to the same index using a
    JSON body in a POST request. The records are returned together
    in whatever order the queries are completed.
    """
    try:
        i = INDEXES[index]

        # decode the body for query parameters
        queries = [_parse_query(q, required=True) for q in qs.q]
        limit = qs.limit
        fmt = qs.fmt

        # discover what the user doesn't have access to see
        restricted, auth_s = profile(restricted_keywords, portal, req)

        # lookup the schema for this index and perform the query
        reader, query_s = profile(
            query.fetch_multi,
            executor,
            engine,
            CONFIG.s3_bucket,
            i,
            queries,
            restricted=restricted,
        )

        # use a zip to limit the total number of records that will be read
        if limit is not None:
            reader.set_limit(limit)

        # the results of the query
        return _fetch_records(reader, index, queries, fmt, query_s=auth_s + query_s)
    except KeyError:
        raise fastapi.HTTPException(
            status_code=400, detail=f'Invalid index: {index}')
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e))


@router.head('/query/{index}')
async def api_test_index(index: str, q: str, req: fastapi.Request):
    """
    Query the database for records matching the query parameter. Don't
    read the records from S3, but instead set the Content-Length to the
    total number of bytes what would be read.
    """
    try:
        i = INDEXES[index]
        qs = _parse_query(q, required=True)

        # lookup the schema for this index and perform the query
        reader, query_s = profile(query.fetch, engine, CONFIG.s3_bucket, i, qs)

        return fastapi.Response(
            headers={'Content-Length': str(reader.bytes_total)})
    except KeyError:
        raise fastapi.HTTPException(
            status_code=400, detail=f'Invalid index: {index}')
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e))


@router.get('/cont', response_class=fastapi.responses.ORJSONResponse)
async def api_cont(token: str):
    """
    Lookup a continuation token and get the next set of records.
    """
    try:
        cont = continuation.lookup_continuation(token)

        # the token is no longer valid
        continuation.remove_continuation(token)

        # execute the continuation callback
        return cont.callback(cont)

    except KeyError:
        raise fastapi.HTTPException(
            status_code=400,
            detail='Invalid, expired, or missing continuation token')
    except ValueError as e:
        raise fastapi.HTTPException(status_code=400, detail=str(e))


def _parse_query(q, required=False):
    """
    Get the `q` query parameter and split it by comma into query parameters
    for a schema query.
    """
    if required and q is None:
        raise ValueError('Missing query parameter')

    # if no query parameter is provided, assume empty string
    return q.split(',') if q else []


def _match_keys(keys, index, qs, limit, page=1, query_s=None):
    """
    Collects up to MATCH_LIMIT keys from a database cursor and then
    return a JSON response object with them.
    """
    fetched, fetch_s = profile(list, itertools.islice(keys, MATCH_LIMIT))

    # create a continuation if there is more data
    token = None if len(
        fetched) < MATCH_LIMIT else continuation.make_continuation(
            callback=
            lambda cont: _match_keys(keys, index, limit, qs, page=page + 1), )

    return {
        'profile': {
            'fetch': fetch_s,
            'query': query_s,
        },
        'index': index,
        'qs': qs,
        'limit': limit,
        'page': page,
        'count': len(fetched),
        'data': list(fetched),
        'continuation': token,
        'nonce': nonce(),
    }


def _fetch_records(reader, index, qs, fmt, page=1, query_s=None):
    """
    Reads up to RESPONSE_LIMIT bytes from a RecordReader, format them,
    and then return a JSON response object with the records.
    """
    bytes_limit = reader.bytes_read + RESPONSE_LIMIT
    restricted_count = reader.restricted_count

    # similar to itertools.takewhile, but keeps the final record
    def take():
        for r in reader.records:
            yield r

            # stop if the byte limit was reached
            if reader.bytes_read > bytes_limit:
                break

    # validate query parameters
    if fmt not in ['r', 'row', 'c', 'col', 'column']:
        raise ValueError('Invalid output format')

    # profile how long it takes to fetch the records from s3
    fetched_records, fetch_s = profile(list, take())
    count = len(fetched_records)

    # transform a list of dictionaries into a dictionary of lists
    if fmt[0] == 'c':
        fetched_records = {
            k: [r.get(k) for r in fetched_records]
            for k in fetched_records[0].keys()
        }

    # create a continuation if there is more data
    token = None if reader.at_end else continuation.make_continuation(
        callback=
        lambda cont: _fetch_records(reader, index, qs, fmt, page=page + 1), )

    # build JSON response
    return {
        'profile': {
            'fetch': fetch_s,
            'query': query_s,
        },
        'index': index,
        'q': qs,
        'count': count,
        'restricted': reader.restricted_count - restricted_count,
        'progress': {
            'bytes_read': reader.bytes_read,
            'bytes_total': reader.bytes_total,
        },
        'page': page,
        'limit': reader.limit,
        'data': fetched_records,
        'continuation': token,
        'nonce': nonce(),
    }
