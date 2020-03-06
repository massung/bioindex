# Bio-Index

Bio-Index is a tool that indexes genomic data stored in [AWS S3][s3] "tables" (typically generated by [Spark][spark]) so that it can be rapidly queried and loaded. It uses a [MySQL][mysql] database to store the indexes and to look up where in [S3][s3] each "record" is located.

The Bio-Index has two entry points: a CLI used for basic CRUD operations and a simple HTTP server and REST API for pure querying.

### Dot Environment

The bio-index uses [python-dotenv][dotenv] (environment variables) for configuration. There are two environment files of importance: `.env` and `.flaskenv`.

The `.env` file contains environment variables for connecting to AWS (if they need to differ from those in the AWS credentials file) and where the `BIOINDEX_CONFIG` file is located, which defaults to `config.json`.

The `.flaskenv` file holds any Flask, server-specific settings (e.g. app module and port number).

### Testing Setup

Once you think you have everything setup, you can test it with the CLI:

```bash
$ python3 -m main test
```

### Preparing Tables

Once everything is setup, you can begin creating or preparing the "table" files in [S3][s3] to be indexed. Each table is expected to be in [JSON-lines][json-lines] format. 

This format is output natively by [Spark][spark] to folders in [S3][s3] when used to process data. For example (using [PySpark][pyspark] on an [AWS EMR][emr] cluster):

```python
df.write.json('s3://my-bucket/folder')
```

The above code would write out many part files to the bucket/path that can now be indexed using the `index` CLI command (details below). However, they will not be well suited for high-performance reading. It is best to always order the output by locus before writing them. This will _dramatically_ improve the performance of Bio-Index:

```python
sorted_snp_df = df.orderBy(['chromosome', 'pos'])
sorted_trait_df = df.orderBy(['phenotype'])
```

### Configuring Indexes

One of the `.env` variables specified is `BIOINDEX_CONFIG` filename. It defaults to `config.json`. This is a JSON file the [MySQL][mysql] RDS instance to use for the indexes, which [S3][s3] bucket will be indexed (and queried) , and all the indexes: the resulting table, sources, and schema. For example:

```json
{
    "s3_bucket": "my-bucket",
    "rds_instance": "my-mysql-instance",
    "tables": {
        "genes": {
            "path": "path/to/genes",
            "schema": "chromosome:start-end"
        }
    }
}
```

The above configuration would result in a table named `genes` being created in the [MySQL][mysql] database `my-mysql-instance`, which contains the indexed records for all the files found in `s3://my-bucket/path/to/genes/`.

The "schema" parameter for the index controls how each record is indexed. It can be in one of three possible formats:

* SNP locus: `chromosome:position`
* Region locus: `chromosome:start-end`
* Value: `column`

The SNP and region locus schemas are the names of the columns of the JSON objects read from each file. For example, if this is your record:

```json
{"snp":"rs3834932", "chr": "12", "pos": 104152227}
``` 

Then your schema would be `chr:pos`, indicating that the chromosome column name is `chr` and the position column name is `pos`.

A value locus schema is an enumeration of possible values to index by. This can be any one key in the JSON object.

### Indexing

Once your configuration has been made, you can run the indexing code for any of the tables using the CLI `index` command along with a comma-separated list of table names. For example, using the example configuration above:

```bash
$ python3 -m main index genes
```

_NOTE: You can also pass `*` as the table name to force indexing of all tables!_

### Querying

Once you've built an index, you can then query the key space and retrieve all the records that overlap a given locus. For example, to query all records in the `genes` key space that overlap a given region:

```bash
$ python3 -m main query genes chr8:100000-101000
{"name": "AC131281.2", "chromosome": "8", "start": 100584, "end": 100728, "ensemblId": "ENSG00000254193", "type": "processed_pseudogene"}
```

The query parameter for the index should always be either a SNP/region locus for a table indexed by locus, or a single value for a table indexed by value.

```bash
$ python3 -m main query phewas rs3834932
{"alt":"CGGGT","beta":-0.0024,"chromosome":"12","n":191764,"pValue":0.8816,"phenotype":"T2D","position":104152227,"reference":"C","stdErr":0.0159,"top":false,"id":"rs3834932","zScore":-0.149}
```

_NOTE: If you'd like to limit the output, just pipe it to `head -n`._

In addition to querying, there are also commands `count` records, fetch `all` records, and to return the enumeration `keys` of a value-schema table.

## Index REST Server

In addition to a CLI, Bio-Index is also a [Flask][flask] server that allows you to query records via REST calls.

### Starting the Server

Create (or edit) the `.flaskenv` file to set the `FLASK_APP` to `server:app` and optionally the `FLASK_PORT` (default is 5000). Then run:

```bash
$ flask run
```

_Note: this assumes `flask` is installed via `pip`. This is also the development environment. If you wish to run this in production, follow the guides on the [Flask][flask] website for doing so._

### REST Queries

The REST server supports all the same commands as the CLI for querying: `query`, `all`, `count`, `keys`, etc. In addition, though, it allows for limits to the number of total results returned and it has a limit on the maximum number of records returned for a single request.

The basic API can be hit with this format:

```
/api/<command>/<index>?q=<query>
```

So, if you'd like to query genes, a REST call like so should do the trick:

```
$ curl http://localhost:5000/api/query/genes?q=chr8:100000-101000
```

You may optionally pass a `limit=N` to limit the maximum number of results returned.

There is also a `format=<column|row>` option (default=row) that can be used to indicate you'd like the results in column-major format.

Each request results in a JSON response that looks like so:

```json
{
    "continuation": null,
    "count": 1,
    "data": [...],
    "index": "genes",
    "limit": null,
    "page": 1,
    "profile": {...},
    "q": "chr8:100000-101000"
}
```

The `count` is the total number of records returned by this query. The `data` is the array of records (if `format=row`) or a dictionary of columns (if `format=column`). The `profile` shows how long the index query took vs. how much time was spent fetching the records from [S3][s3].

If the `continutation` value is non-null, then it is a string, which is a token indicating there are more records left to be returned. They can be retrieved using the `/api/cont?token=<token>` end-point.

### Dependencies

* [Python 3.6+][python]
* [setuptools][setuptools]
* [python-dotenv][dotenv]
* [click][click]
* [flask][flask]
* [boto3][boto3]
* [sqlalchemy][sqlalchemy]
* [mysqlclient][mysqlclient]
* [enlighten][enlighten]

# fin.

[python]: https://www.python.org/
[setuptools]: https://setuptools.readthedocs.io/en/latest/
[dotenv]: https://saurabh-kumar.com/python-dotenv/
[mysql]: https://www.mysql.com/
[s3]: https://docs.aws.amazon.com/AmazonS3/latest/dev/Welcome.html
[emr]: https://aws.amazon.com/emr/
[click]: https://click.palletsprojects.com/en/7.x/quickstart/
[enlighten]: https://python-enlighten.readthedocs.io/en/stable/
[flask]: https://www.palletsprojects.com/p/flask/
[boto3]: https://aws.amazon.com/sdk-for-python/
[sqlalchemy]: http://www.sqlalchemy.org/
[mysqlclient]: https://pypi.org/project/mysqlclient/
[spark]: https://spark.apache.org/
[pyspark]: https://spark.apache.org/docs/latest/api/python/pyspark.html
[json-lines]: http://jsonlines.org/examples/
