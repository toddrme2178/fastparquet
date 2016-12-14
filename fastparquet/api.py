"""parquet - read parquet files."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import operator
import os
import re
import struct

import numpy as np
import pandas as pd
import thriftpy

from .core import read_thrift
from .thrift_structures import parquet_thrift
from . import core, schema, converted_types, encoding, writer, df_empty
from .util import (default_open, ParquetException, sep_from_open, val_to_num,
                   ensure_bytes)


class ParquetFile(object):
    """The metadata of a parquet file or collection

    Reads the metadata (row-groups and schema definition) and provides
    methods to extract the data from the files.

    Parameters
    ----------
    fn: path/URL string
        Location of the data. If a directory, will attempt to read a file
        "_metadata" within that directory.
    verify: bool [False]
        test file start/end byte markers
    open_with: function
        With the signature `func(path, mode)`, returns a context which
        evaluated to a file open for reading. Defaults to the built-in `open`.
    sep: string [`os.sep`]
        Path separator to use, if data is in multiple files.
    """
    def __init__(self, fn, verify=False, open_with=default_open,
                 sep=os.sep):
        try:
            fn2 = sep.join([fn, '_metadata'])
            self.fn = fn2
            with open_with(fn2, 'rb') as f:
                self._parse_header(f, verify)
            fn = fn2
        except (IOError, OSError):
            self.fn = fn
            with open_with(fn, 'rb') as f:
                self._parse_header(f, verify)
        if all(rg.columns[0].file_path is None for rg in self.row_groups):
            self.file_scheme = 'simple'
        elif all(rg.columns[0].file_path is not None for rg in self.row_groups):
            self.file_scheme = 'hive'
        else:
            self.file_scheme = 'mixed'
        self.open = open_with
        self.sep = sep
        self._read_partitions()

    def _parse_header(self, f, verify=True):
        try:
            f.seek(0)
            if verify:
                assert f.read(4) == b'PAR1'
            f.seek(-8, 2)
            head_size = struct.unpack('<i', f.read(4))[0]
            if verify:
                assert f.read() == b'PAR1'
        except (AssertionError, struct.error):
            raise ParquetException('File parse failed: %s' % self.fn)

        f.seek(-(head_size+8), 2)
        try:
            fmd = read_thrift(f, parquet_thrift.FileMetaData)
        except thriftpy.transport.TTransportException:
            raise ParquetException('Metadata parse failed: %s' %
                                         self.fn)
        self.fmd = fmd
        self.head_size = head_size
        self.version = fmd.version
        self.schema = fmd.schema
        self.row_groups = fmd.row_groups or []
        self.key_value_metadata = fmd.key_value_metadata
        self.created_by = fmd.created_by
        self.group_files = {}
        for i, rg in enumerate(self.row_groups):
            for chunk in rg.columns:
                self.group_files.setdefault(i, set()).add(chunk.file_path)
        self.helper = schema.SchemaHelper(self.schema)
        self.selfmade = self.created_by == "fastparquet-python"

    @property
    def columns(self):
        """ Column names """
        return [f.name for f in self.schema if f.num_children is None]

    @property
    def statistics(self):
        return statistics(self)

    def _read_partitions(self):
        cats = {}
        for rg in self.row_groups:
            for col in rg.columns:
                partitions = re.findall("([a-zA-Z_]+)=([^/]+)/",
                                        col.file_path or "")
                for key, val in partitions:
                    cats.setdefault(key, set()).add(val)
        self.cats = {key: list([val_to_num(x) for x in v])
                     for key, v in cats.items()}

    def row_group_filename(self, rg):
        if rg.columns[0].file_path:
            return self.sep.join([os.path.dirname(self.fn),
                                  rg.columns[0].file_path])
        else:
            return self.fn


    def read_row_group_file(self, rg, columns, categories, index=None,
                            assign=None):
        """ Open file for reading, and process it as a row-group """
        fn = self.row_group_filename(rg)
        return core.read_row_group_file(
                fn, rg, columns, categories, self.helper, self.cats,
                open=self.open, selfmade=self.selfmade, index=index,
                assign=assign)

    def read_row_group(self, rg, columns, categories, infile=None,
                       index=None, assign=None):
        """
        Access row-group in a file and read some columns into a data-frame.
        """
        return core.read_row_group(
                infile, rg, columns, categories, self.helper, self.cats,
                self.selfmade, index=index, assign=assign)

    def grab_cats(self, columns, row_group_index=0):
        """ Read dictionaries of first row_group

        Used to correctly create metadata for categorical dask dataframes.
        Could be used to check that the same dictionary is used throughout
        the data.

        Parameters
        ----------
        columns: list
            Column names to load
        row_group_index: int (0)
            Row group to load from

        Returns
        -------
        {column: [list, of, values]}
        """
        if len(columns) == 0:
            return {}
        rg = self.row_groups[row_group_index]
        ofname = self.row_group_filename(rg)
        out = {}

        with self.open(ofname, 'rb') as f:
            for column in rg.columns:
                name = ".".join(column.meta_data.path_in_schema)
                if name not in columns:
                    continue
                out[name] = core.read_col(column, self.helper, f,
                                          grab_dict=True)
        return out

    def filter_row_groups(self, filters):
        """
        Select row groups using set of filters

        Parameters
        ----------
        filters: list of tuples
            See ``filter_out_cats`` and ``filter_out_stats``

        Returns
        -------
        Filtered list of row groups
        """
        return [rg for rg in self.row_groups if
                not(filter_out_stats(rg, filters, self.helper)) and
                not(filter_out_cats(rg, filters))]

    def iter_row_groups(self, columns=None, categories=None, filters=[],
                        index=None, assign=None):
        """
        Read data from parquet into a Pandas dataframe.

        Parameters
        ----------
        columns: list of names or `None`
            Column to load (see `ParquetFile.columns`). Any columns in the
            data not in this list will be ignored. If `None`, read all columns.
        categories: list, dict or `None`
            If a column is encoded using dictionary encoding in every row-group
            and its name is also in this list, it will generate a Pandas
            Category-type column, potentially saving memory and time. If a
            dict {col: int}, the value indicates the number of categories,
            so that the optimal data-dtype can be allocated.
        filters: list of tuples
            Filter syntax: [(column, op, val), ...],
            where op is [==, >, >=, <, <=, !=, in, not in]
        index: string or None
            Column to assign to the index. If None, index is simple sequential
            integers.
        assign: dict {cols: array}
            Pre-allocated memory to write to. If None, will allocate memory
            here.

        Returns
        -------
        Generator yielding one Pandas data-frame per row-group
        """
        columns = columns or self.columns
        rgs = self.filter_row_groups(filters)
        columns = columns or self.columns
        if all(column.file_path is None for rg in self.row_groups
               for column in rg.columns):
            with self.open(self.fn) as f:
                for rg in rgs:
                    df, views = self.pre_allocate(rg.num_rows, columns,
                                                  categories, index)
                    self.read_row_group(rg, columns, categories, infile=f,
                                        index=index, assign=views)
                    yield df
        else:
            for rg in rgs:
                df, views = self.pre_allocate(rg.num_rows, columns,
                                              categories, index)
                self.read_row_group_file(rg, columns, categories, index,
                                         assign=views)
                yield df

    def to_pandas(self, columns=None, categories=None, filters=[],
                  index=None):
        """
        Read data from parquet into a Pandas dataframe.

        Parameters
        ----------
        columns: list of names or `None`
            Column to load (see `ParquetFile.columns`). Any columns in the
            data not in this list will be ignored. If `None`, read all columns.
        categories: list, dict or `None`
            If a column is encoded using dictionary encoding in every row-group
            and its name is also in this list, it will generate a Pandas
            Category-type column, potentially saving memory and time. If a
            dict {col: int}, the value indicates the number of categories,
            so that the optimal data-dtype can be allocated.
        filters: list of tuples
            Filter syntax: [(column, op, val), ...],
            where op is [==, >, >=, <, <=, !=, in, not in]
        index: string or None
            Column to assign to the index. If None, index is simple sequential
            integers.

        Returns
        -------
        Pandas data-frame
        """
        rgs = self.filter_row_groups(filters)
        size = sum(rg.num_rows for rg in rgs)
        columns = columns or self.columns
        df, views = self.pre_allocate(size, columns, categories, index)
        start = 0
        if self.file_scheme == 'simple':
            with self.open(self.fn) as f:
                for rg in rgs:
                    parts = {name: (v if name.endswith('-catdef')
                                    else v[start:start + rg.num_rows])
                             for (name, v) in views.items()}
                    self.read_row_group(rg, columns, categories, infile=f,
                                        index=index, assign=parts)
                    start += rg.num_rows
        else:
            for rg in rgs:
                parts = {name: (v if name.endswith('-catdef')
                                else v[start:start + rg.num_rows])
                         for (name, v) in views.items()}
                self.read_row_group_file(rg, columns, categories, index,
                                         assign=parts)
                start += rg.num_rows
        return df

    def pre_allocate(self, size, columns, categories, index):
        cols = [c for c in columns if index != c]
        categories = categories or {}
        cats = self.cats.copy()
        if isinstance(categories, dict):
            cats.update(categories)
        dtypes = ['category' if c in categories else self.dtypes[c]
                  for c in cols]
        index_type = ('category' if index in categories
                      else self.dtypes.get(index, None))
        cols.extend(self.cats)
        dtypes.extend(['category'] * len(self.cats))
        df, views = df_empty.empty(dtypes, size, cols=cols, index_name=index,
                                   index_type=index_type, cats=self.cats)
        return df, views

    @property
    def count(self):
        """ Total number of rows """
        return sum(rg.num_rows for rg in self.row_groups)

    @property
    def info(self):
        """ Some metadata details """
        return {'name': self.fn, 'columns': self.columns,
                'categories': list(self.cats), 'rows': self.count}

    @property
    def dtypes(self):
        """ Implied types of the columns in the schema """
        dtype = {f.name: converted_types.typemap(f)
                 for f in self.schema if f.num_children is None}
        for col, dt in dtype.copy().items():
            if dt.kind == 'i':
                num_nulls = 0
                for rg in self.row_groups:
                    chunks = [c for c in rg.columns
                              if c.meta_data.path_in_schema[-1] == col]
                    for chunk in chunks:
                        if chunk.meta_data.statistics is not None:
                            num_nulls += (
                                chunk.meta_data.statistics.null_count or 0)
                if num_nulls:
                    dtype[col] = np.dtype('f')
        for cat in self.cats:
            dtype[cat] = "category"
            # pd.Series(self.cats[cat]).map(val_to_num).dtype
        return dtype

    def __str__(self):
        return "<Parquet File: %s>" % self.info

    __repr__ = __str__


def filter_out_stats(rg, filters, helper):
    """
    According to the filters, should this row-group be excluded

    Considers the statistics included in the metadata of this row-group

    Parameters
    ----------
    rg: thrift RowGroup structure
    filters: list of 3-tuples
        Structure of each tuple: (column, op, value) where op is one of
        ['==', '!=', '<', '<=', '>', '>=', 'in', 'not in'] and value is
        appropriate for the column in question

    Returns
    -------
    True or False
    """
    if len(filters) == 0:
        return False
    for column in rg.columns:
        vmax, vmin = None, None
        name = ".".join(column.meta_data.path_in_schema)
        app_filters = [f[1:] for f in filters if f[0] == name]
        for op, val in app_filters:
            se = helper.schema_element(name)
            if column.meta_data.statistics is not None:
                s = column.meta_data.statistics
                if s.max is not None:
                    b = s.max if isinstance(s.max, bytes) else bytes(
                            s.max, 'ascii')
                    vmax = encoding.read_plain(b, column.meta_data.type, 1)
                    if se.converted_type:
                        vmax = converted_types.convert(vmax, se)
                if s.min is not None:
                    b = s.min if isinstance(s.min, bytes) else bytes(
                            s.min, 'ascii')
                    vmin = encoding.read_plain(b, column.meta_data.type, 1)
                    if se.converted_type:
                        vmin = converted_types.convert(vmin, se)
                out = filter_val(op, val, vmin, vmax)
                if out is True:
                    return True
    return False


def statistics(obj):
    """
    Return per-column statistics for a ParquerFile

    Parameters
    ----------
    obj: ParquetFile

    Returns
    -------
    dictionary mapping stats (min, max, distinct_count, null_count) to column
    names to lists of values.  ``None``s used if no statistics found.

    Examples
    --------
    >>> statistics(my_parquet_file)
    {'min': {'x': [1, 4], 'y': [5, 3]},
     'max': {'x': [2, 6], 'y': [8, 6]},
     'distinct_count': {'x': [None, None], 'y': [None, None]},
     'null_count': {'x': [0, 3], 'y': [0, 0]}}
    """
    if isinstance(obj, parquet_thrift.ColumnChunk):
        md = obj.meta_data
        s = obj.meta_data.statistics
        rv = {}
        if not s:
            return rv
        if s.max is not None:
            rv['max'] = encoding.read_plain(ensure_bytes(s.max), md.type, 1)[0]
        if s.min is not None:
            rv['min'] = encoding.read_plain(ensure_bytes(s.min), md.type, 1)[0]
        if s.null_count is not None:
            rv['null_count'] = s.null_count
        if s.distinct_count is not None:
            rv['distinct_count'] = s.distinct_count
        return rv

    if isinstance(obj, parquet_thrift.RowGroup):
        return {'.'.join(c.meta_data.path_in_schema): statistics(c)
                for c in obj.columns}

    if isinstance(obj, ParquetFile):
        L = list(map(statistics, obj.row_groups))
        names = obj.columns
        d = {n: {col: [item[col].get(n, None) for item in L]
                 for col in obj.columns}
             for n in ['min', 'max', 'null_count', 'distinct_count']}
        helper = schema.SchemaHelper(obj.schema)
        for col in obj.row_groups[0].columns:
            column = '.'.join(col.meta_data.path_in_schema)
            se = helper.schema_element(column)
            if se.converted_type is not None:
                for name in ['min', 'max']:
                    d[name][column] = (
                        [None] if d[name][column] is None or None in d[name][column]
                        else list(converted_types.convert(d[name][column], se))
                        )
        return d


def sorted_partitioned_columns(pf):
    """
    The columns that are known to be sorted partition-by-partition

    They may not be sorted within each partition, but all elements in one
    row group are strictly greater than all elements in previous row groups.

    Examples
    --------
    >>> sorted_partitioned_columns(pf)
    {'id': {'min': [1, 5, 10], 'max': [4, 9, 20]}}

    Returns
    -------
    A set of column names

    See Also
    --------
    statistics
    """
    s = statistics(pf)
    columns = pf.columns
    out = dict()
    for c in columns:
        min, max = s['min'][c], s['max'][c]
        if any(x is None for x in min + max):
            continue
        if (sorted(min) == min and
            sorted(max) == max and
            all(mx < mn for mx, mn in zip(max[:-1], min[1:]))):
            out[c] = {'min': min, 'max': max}
    return out



def filter_out_cats(rg, filters):
    """
    According to the filters, should this row-group be excluded

    Considers the partitioning category applicable to this row-group

    Parameters
    ----------
    rg: thrift RowGroup structure
    filters: list of 3-tuples
        Structure of each tuple: (column, op, value) where op is one of
        ['==', '!=', '<', '<=', '>', '>=', 'in', 'not in'] and value is
        appropriate for the column in question

    Returns
    -------
    True or False
    """
    if len(filters) == 0:
        return False
    partitions = re.findall("([a-zA-Z_]+)=([^/]+)/",
                            rg.columns[0].file_path)
    pairs = [(p[0], val_to_num(p[1])) for p in partitions]
    for cat, v in pairs:

        app_filters = [f[1:] for f in filters if f[0] == cat]
        for op, val in app_filters:
            out = filter_val(op, val, v, v)
            if out is True:
                return True
    return False


def filter_val(op, val, vmin=None, vmax=None):
    """
    Perform value comparison for filtering

    op: ['==', '!=', '<', '<=', '>', '>=', 'in', 'not in']
    val: appropriate value
    vmin, vmax: the range to compare within

    Returns
    -------
    True or False
    """
    if vmin is not None:
        if op in ['==', '>='] and val > vmax:
            return True
        if op == '>' and val >= vmax:
            return True
        if op == 'in' and min(val) > vmax:
            return True
    if vmax is not None:
        if op in ['==', '<='] and val < vmin:
            return True
        if op == '<' and val <= vmin:
            return True
        if op == 'in' and max(val) < vmin:
            return True
    if (op == '!=' and vmax is not None and vmin is not None and
            vmax == vmin and val == vmax):
        return True
    if (op == 'not in' and vmax is not None and vmin is not None and
            vmax == vmin and vmax in val):
        return True

    # keep this row_group
    return False
