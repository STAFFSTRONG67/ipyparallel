"""A TaskRecord backend using sqlite3"""

# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.
import json
import os

try:
    import cPickle as pickle
except ImportError:
    import pickle

from datetime import datetime

try:
    import sqlite3
except ImportError:
    sqlite3 = None

from dateutil.parser import parse as dateutil_parse

try:
    from jupyter_client.jsonutil import json_default
except ImportError:
    from jupyter_client.jsonutil import date_default as json_default

from tornado import ioloop
from traitlets import Dict, Instance, List, Unicode

from ..util import ensure_timezone, extract_dates
from .dictdb import BaseDB

# -----------------------------------------------------------------------------
# SQLite operators, adapters, and converters
# -----------------------------------------------------------------------------

operators = {
    '$lt': "<",
    '$gt': ">",
    # null is handled weird with ==,!=
    '$eq': "=",
    '$ne': "!=",
    '$lte': "<=",
    '$gte': ">=",
    '$in': ('=', ' OR '),
    '$nin': ('!=', ' AND '),
    # '$all': None,
    # '$mod': None,
    # '$exists' : None
}
null_operators = {
    '=': "IS NULL",
    '!=': "IS NOT NULL",
}


def _adapt_dict(d):
    return json.dumps(d, default=json_default)


def _convert_dict(ds):
    if ds is None:
        return ds
    else:
        if isinstance(ds, bytes):
            # If I understand the sqlite doc correctly, this will always be utf8
            ds = ds.decode('utf8')
        return extract_dates(json.loads(ds))


def _adapt_bufs(bufs):
    # this is *horrible*
    # copy buffers into single list and pickle it:
    if bufs and isinstance(bufs[0], (bytes, memoryview)):
        return sqlite3.Binary(
            pickle.dumps([memoryview(buf).tobytes() for buf in bufs], -1)
        )
    elif bufs:
        return bufs
    else:
        return None


def _convert_bufs(bs):
    if bs is None:
        return []
    else:
        return pickle.loads(bytes(bs))


def _adapt_timestamp(dt):
    """Adapt datetime to text"""
    return ensure_timezone(dt).isoformat()


def _convert_timestamp(s):
    """Adapt text timestamp to datetime"""
    return ensure_timezone(dateutil_parse(s))


# -----------------------------------------------------------------------------
# SQLiteDB class
# -----------------------------------------------------------------------------


class SQLiteDB(BaseDB):
    """SQLite3 TaskRecord backend."""

    filename = Unicode(
        'tasks.db',
        config=True,
        help="""The filename of the sqlite task database. [default: 'tasks.db']""",
    )
    location = Unicode(
        '',
        config=True,
        help="""The directory containing the sqlite task database.  The default
        is to use the cluster_dir location.""",
    )
    table = Unicode(
        "ipython-tasks",
        config=True,
        help="""The SQLite Table to use for storing tasks for this session. If unspecified,
        a new table will be created with the Hub's IDENT.  Specifying the table will result
        in tasks from previous sessions being available via Clients' db_query and
        get_result methods.""",
    )

    if sqlite3 is not None:
        _db = Instance('sqlite3.Connection', allow_none=True)
    else:
        _db = None
    # the ordered list of column names
    _keys = List(
        [
            'msg_id',
            'header',
            'metadata',
            'content',
            'buffers',
            'submitted',
            'client_uuid',
            'engine_uuid',
            'started',
            'completed',
            'resubmitted',
            'received',
            'result_header',
            'result_metadata',
            'result_content',
            'result_buffers',
            'queue',
            'execute_input',
            'execute_result',
            'error',
            'stdout',
            'stderr',
        ]
    )
    # sqlite datatypes for checking that db is current format
    _types = Dict(
        {
            'msg_id': 'text',
            'header': 'dict text',
            'metadata': 'dict text',
            'content': 'dict text',
            'buffers': 'bufs blob',
            'submitted': 'timestamp',
            'client_uuid': 'text',
            'engine_uuid': 'text',
            'started': 'timestamp',
            'completed': 'timestamp',
            'resubmitted': 'text',
            'received': 'timestamp',
            'result_header': 'dict text',
            'result_metadata': 'dict text',
            'result_content': 'dict text',
            'result_buffers': 'bufs blob',
            'queue': 'text',
            'execute_input': 'text',
            'execute_result': 'text',
            'error': 'text',
            'stdout': 'text',
            'stderr': 'text',
        }
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if sqlite3 is None:
            raise ImportError("SQLiteDB requires sqlite3")
        if not self.table:
            # use session, and prefix _, since starting with # is illegal
            self.table = '_' + self.session.replace('-', '_')
        if not self.location:
            # get current profile
            from IPython.core.application import BaseIPythonApplication

            if BaseIPythonApplication.initialized():
                app = BaseIPythonApplication.instance()
                if app.profile_dir is not None:
                    self.location = app.profile_dir.location
                else:
                    self.location = '.'
            else:
                self.location = '.'
        self._init_db()

        # register db commit as 2s periodic callback
        # to prevent clogging pipes
        # assumes we are being run in a zmq ioloop app
        self._commit_callback = pc = ioloop.PeriodicCallback(self._db.commit, 2000)
        pc.start()

    def close(self):
        self._commit_callback.stop()
        self._db.commit()
        self._db.close()

    def _defaults(self, keys=None):
        """create an empty record"""
        d = {}
        keys = self._keys if keys is None else keys
        for key in keys:
            d[key] = None
        return d

    def _check_table(self):
        """Ensure that an incorrect table doesn't exist

        If a bad (old) table does exist, return False
        """
        cursor = self._db.execute(f"PRAGMA table_info('{self.table}')")
        lines = cursor.fetchall()
        if not lines:
            # table does not exist
            return True
        types = {}
        keys = []
        for line in lines:
            keys.append(line[1])
            types[line[1]] = line[2]
        if self._keys != keys:
            # key mismatch
            self.log.warning('keys mismatch')
            return False
        for key in self._keys:
            if types[key] != self._types[key]:
                self.log.warning(
                    f'type mismatch: {key}: {types[key]} != {self._types[key]}'
                )
                return False
        return True

    def _init_db(self):
        """Connect to the database and get new session number."""
        # register adapters
        sqlite3.register_adapter(dict, _adapt_dict)
        sqlite3.register_converter('dict', _convert_dict)
        sqlite3.register_adapter(list, _adapt_bufs)
        sqlite3.register_converter('bufs', _convert_bufs)
        sqlite3.register_adapter(datetime, _adapt_timestamp)
        sqlite3.register_converter('timestamp', _convert_timestamp)
        # connect to the db
        dbfile = os.path.join(self.location, self.filename)
        self._db = sqlite3.connect(
            dbfile,
            detect_types=sqlite3.PARSE_DECLTYPES,
            # isolation_level = None)#,
            cached_statements=64,
        )
        # print dir(self._db)
        first_table = previous_table = self.table
        i = 0
        while not self._check_table():
            i += 1
            self.table = f"{first_table}_{i}"
            self.log.warning(
                f"Table {previous_table} exists and doesn't match db format, trying {self.table}"
            )
            previous_table = self.table

        self._db.execute(
            f"""CREATE TABLE IF NOT EXISTS '{self.table}'
                (msg_id text PRIMARY KEY,
                header dict text,
                metadata dict text,
                content dict text,
                buffers bufs blob,
                submitted timestamp,
                client_uuid text,
                engine_uuid text,
                started timestamp,
                completed timestamp,
                resubmitted text,
                received timestamp,
                result_header dict text,
                result_metadata dict text,
                result_content dict text,
                result_buffers bufs blob,
                queue text,
                execute_input text,
                execute_result text,
                error text,
                stdout text,
                stderr text)
                """
        )
        self._db.commit()

    def _dict_to_list(self, d):
        """turn a mongodb-style record dict into a list."""

        return [d[key] for key in self._keys]

    def _list_to_dict(self, line, keys=None):
        """Inverse of dict_to_list"""
        keys = self._keys if keys is None else keys
        d = self._defaults(keys)
        for key, value in zip(keys, line):
            d[key] = value

        return d

    def _render_expression(self, check):
        """Turn a mongodb-style search dict into an SQL query."""
        expressions = []
        args = []

        skeys = set(check.keys())
        skeys.difference_update(set(self._keys))
        skeys.difference_update({'buffers', 'result_buffers'})
        if skeys:
            raise KeyError(f"Illegal testing key(s): {skeys}")

        for name, sub_check in check.items():
            if isinstance(sub_check, dict):
                for test, value in sub_check.items():
                    try:
                        op = operators[test]
                    except KeyError:
                        raise KeyError(f"Unsupported operator: {test!r}")
                    if isinstance(op, tuple):
                        op, join = op

                    if value is None and op in null_operators:
                        expr = f"{name} {null_operators[op]}"
                    else:
                        expr = f"{name} {op} ?"
                        if isinstance(value, (tuple, list)):
                            if op in null_operators and any([v is None for v in value]):
                                # equality tests don't work with NULL
                                raise ValueError(
                                    f"Cannot use {test!r} test with NULL values on SQLite backend"
                                )
                            expr = f'( {join.join([expr] * len(value))} )'
                            args.extend(value)
                        else:
                            args.append(value)
                    expressions.append(expr)
            else:
                # it's an equality check
                if sub_check is None:
                    expressions.append(f"{name} IS NULL")
                else:
                    expressions.append(f"{name} = ?")
                    args.append(sub_check)

        expr = " AND ".join(expressions)
        return expr, args

    def add_record(self, msg_id, rec):
        """Add a new Task Record, by msg_id."""
        d = self._defaults()
        d.update(rec)
        d['msg_id'] = msg_id
        line = self._dict_to_list(d)
        tups = '({})'.format(','.join(['?'] * len(line)))
        self._db.execute(f"INSERT INTO '{self.table}' VALUES {tups}", line)
        # self._db.commit()

    def get_record(self, msg_id):
        """Get a specific Task Record, by msg_id."""
        cursor = self._db.execute(
            f"""SELECT * FROM '{self.table}' WHERE msg_id==?""", (msg_id,)
        )
        line = cursor.fetchone()
        if line is None:
            raise KeyError(f"No such msg: {msg_id!r}")
        return self._list_to_dict(line)

    def update_record(self, msg_id, rec):
        """Update the data in an existing record."""
        query = f"UPDATE '{self.table}' SET "
        sets = []
        keys = sorted(rec.keys())
        values = []
        for key in keys:
            sets.append(f'{key} = ?')
            values.append(rec[key])
        query += ', '.join(sets)
        query += ' WHERE msg_id == ?'
        values.append(msg_id)
        self._db.execute(query, values)
        # self._db.commit()

    def drop_record(self, msg_id):
        """Remove a record from the DB."""
        self._db.execute(f"""DELETE FROM '{self.table}' WHERE msg_id==?""", (msg_id,))
        # self._db.commit()

    def drop_matching_records(self, check):
        """Remove a record from the DB."""
        expr, args = self._render_expression(check)
        query = f"DELETE FROM '{self.table}' WHERE {expr}"
        self._db.execute(query, args)
        # self._db.commit()

    def find_records(self, check, keys=None):
        """Find records matching a query dict, optionally extracting subset of keys.

        Returns list of matching records.

        Parameters
        ----------
        check : dict
            mongodb-style query argument
        keys : list of strs [optional]
            if specified, the subset of keys to extract.  msg_id will *always* be
            included.
        """
        if keys:
            bad_keys = [key for key in keys if key not in self._keys]
            if bad_keys:
                raise KeyError(f"Bad record key(s): {bad_keys}")

        if keys:
            # ensure msg_id is present and first:
            if 'msg_id' in keys:
                keys.remove('msg_id')
            keys.insert(0, 'msg_id')
            req = ', '.join(keys)
        else:
            req = '*'
        expr, args = self._render_expression(check)
        query = f"""SELECT {req} FROM '{self.table}' WHERE {expr}"""
        cursor = self._db.execute(query, args)
        matches = cursor.fetchall()
        records = []
        for line in matches:
            rec = self._list_to_dict(line, keys)
            records.append(rec)
        return records

    def get_history(self):
        """get all msg_ids, ordered by time submitted."""
        query = f"""SELECT msg_id FROM '{self.table}' ORDER by submitted ASC"""
        cursor = self._db.execute(query)
        # will be a list of length 1 tuples
        return [tup[0] for tup in cursor.fetchall()]


__all__ = ['SQLiteDB']
