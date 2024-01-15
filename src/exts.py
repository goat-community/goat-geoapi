"""
---------------------------------------------------------------------------------
This code is based on or incorporates material from the project:
https://github.com/developmentseed/tipg

The original code/repository is licensed under MIT License.
---------------------------------------------------------------------------------
"""

from typing import Dict, Optional, List, Tuple, Callable, Any
from buildpg import clauses, funcs as pg_funcs, RawDangerous as raw, logic
from tipg.collections import Column
from tipg.dependencies import Query
from pygeofilter.parsers.cql2_json import parse as cql2_json_parser
from typing_extensions import Annotated
from pygeofilter.ast import AstType
from starlette.requests import Request
import json
from morecantile import Tile, TileMatrixSet
from tipg.errors import (
    InvalidDatetimeColumnName,
    InvalidPropertyName,
    MissingDatetimeColumn,
)
from tipg.filter.evaluate import to_filter
from tipg.filter.filters import bbox_to_wkt
from inspect import signature
from buildpg.funcs import any
from buildpg.logic import Func

# TODO: Add test for the functions


# These are the function that need to be patched.
def _from(self, function_parameters: Optional[Dict[str, str]]):
    """Construct a FROM statement for the table."""
    if self.type == "Function":
        if not function_parameters:
            return clauses.From(self.id) + raw("()")
        params = []
        for p in self.parameters:
            if p.name in function_parameters:
                params.append(
                    pg_funcs.cast(
                        pg_funcs.cast(function_parameters[p.name], "text"),
                        p.type,
                    )
                )
        return clauses.From(logic.Func(self.id, *params))
    return clauses.From(self.dbschema + "." + self.table)


def real_columns(properties: Optional[List[str]]) -> List[str]:
    """Return table columns optionally filtered to only include columns from properties."""
    if properties in [[], [""]]:
        return []

    cols = [
        c.description for c in properties if c.type not in ["geometry", "geography"]
    ]

    return cols


def get_column(self, property_name: str) -> Optional[Column]:
    """Return column info."""
    for p in self.properties:
        if p.description == property_name:
            return p

    return None


def _select_no_geo(self, properties: Optional[List[str]], addid: bool = True):
    """Construct a SELECT statement for the table."""

    nocomma = False
    columns = self.columns(properties)
    old_columns = real_columns(self.properties)
    if columns:
        select_query = "SELECT "
        for i, column in enumerate(columns):
            select_query = select_query + old_columns[i] + " AS " + column + ", "
        select_query = select_query[:-2]
        sel = logic.as_sql_block(raw(select_query))
    else:
        sel = logic.as_sql_block(raw("SELECT "))
        nocomma = True

    if addid:
        if self.id_column:
            id_clause = logic.V(self.id_column.name).as_("tipg_id")
        else:
            id_clause = raw(" ROW_NUMBER () OVER () AS tipg_id ")
        if nocomma:
            sel = clauses.Clauses(sel, id_clause)
        else:
            sel = sel.comma(id_clause)

    return logic.as_sql_block(sel)


def _where(  # noqa: C901
    self,
    ids: Optional[List[str]] = None,
    datetime: Optional[List[str]] = None,
    bbox: Optional[List[float]] = None,
    properties: Optional[List[Tuple[str, Any]]] = None,
    cql: Optional[AstType] = None,
    geom: Optional[str] = None,
    dt: Optional[str] = None,
    tile: Optional[Tile] = None,
    tms: Optional[TileMatrixSet] = None,
):
    """Construct WHERE query."""
    wheres = [logic.S(True)]

    # `ids` filter
    if ids is not None:
        if len(ids) == 1:
            wheres.append(
                logic.V(self.id_column)
                == pg_funcs.cast(
                    pg_funcs.cast(ids[0], "text"), self.id_column_info.type
                )
            )
        else:
            w = [
                logic.V(self.id_column)
                == logic.S(
                    pg_funcs.cast(pg_funcs.cast(i, "text"), self.id_column_info.type)
                )
                for i in ids
            ]
            wheres.append(pg_funcs.OR(*w))

    # `properties filter
    if properties is not None:
        w = []
        for prop, val in properties:
            col = self.get_column(prop)
            if not col:
                raise InvalidPropertyName(f"Invalid property name: {prop}")

            w.append(
                logic.V(col.name)
                == logic.S(pg_funcs.cast(pg_funcs.cast(val, "text"), col.type))
            )

        if w:
            wheres.append(pg_funcs.AND(*w))

    # `bbox` filter
    geometry_column = self.get_geometry_column(geom)
    if bbox is not None and geometry_column is not None:
        wheres.append(
            logic.Func(
                "ST_Intersects",
                logic.S(bbox_to_wkt(bbox)),
                logic.V(geometry_column.name),
            )
        )

    # `datetime` filter
    if datetime:
        if not self.datetime_columns:
            raise MissingDatetimeColumn(
                "Must have timestamp typed column to filter with datetime."
            )

        datetime_column = self.get_datetime_column(dt)
        if not datetime_column:
            raise InvalidDatetimeColumnName(f"Invalid Datetime Column: {dt}.")

        wheres.append(self._datetime_filter_to_sql(datetime, datetime_column.name))

    # `CQL` filter
    if cql is not None:
        wheres.append(to_filter(cql, [p.description for p in self.properties]))

    if tile and tms and geometry_column:
        # Get Tile Bounds in Geographic CRS (usually epsg:4326)
        left, bottom, right, top = tms.bounds(tile)

        # Truncate bounds to the max TMS bbox
        left, bottom = tms.truncate_lnglat(left, bottom)
        right, top = tms.truncate_lnglat(right, top)

        wheres.append(
            logic.Func(
                "ST_Intersects",
                logic.Func(
                    "ST_Transform",
                    logic.Func(
                        "ST_Segmentize",
                        logic.Func(
                            "ST_MakeEnvelope",
                            left,
                            bottom,
                            right,
                            top,
                            4326,
                        ),
                        right - left,
                    ),
                    pg_funcs.cast(geometry_column.srid, "int"),
                ),
                logic.V(geometry_column.name),
            )
        )

    return clauses.Where(pg_funcs.AND(*wheres))


def replace_properties(data, replacements):
    """Replace the property value in CQL2 JSON with the actual column name."""
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "property" and value in replacements:
                data[key] = replacements[value]
            else:
                replace_properties(value, replacements)
    elif isinstance(data, list):
        for item in data:
            replace_properties(item, replacements)


def format_to_uuid(hex_string):
    if len(hex_string) != 32:
        raise ValueError("The input string must be exactly 32 characters long.")

    return f"{hex_string[:8]}-{hex_string[8:12]}-{hex_string[12:16]}-{hex_string[16:20]}-{hex_string[20:]}"


def filter_query(
    request: Request,
    query: Annotated[
        Optional[str], Query(description="CQL2 Filter", alias="filter")
    ] = None,
) -> Optional[AstType]:
    """Parse Filter Query."""

    # Get layer_id from collectionId
    collectionId = request.path_params["collectionId"].split(".")[1]
    filter_layer_id = {
        "op": "=",
        "args": [{"property": "layer_id"}, format_to_uuid(collectionId)],
    }

    if query is not None:
        layer = request.app.state.collection_catalog["collections"].get(
            request.path_params["collectionId"]
        )
        column_mapping = {}
        for column in layer.properties:
            column_mapping[column.name] = column.description
        # Replace the properties
        cql_dict = json.loads(query)
        replace_properties(cql_dict, column_mapping)

        # Add layer_id filter
        cql_dict = {"op": "and", "args": [cql_dict, filter_layer_id]}

        data = cql2_json_parser(json.dumps(cql_dict))
        return data
    else:
        cql_dict = filter_layer_id

    return cql2_json_parser(json.dumps(cql_dict))


class Operator:
    """Filter Operators."""

    OPERATORS: Dict[str, Callable] = {
        "is_null": lambda f, a=None: f.is_(None),
        "is_not_null": lambda f, a=None: f.isnot(None),
        "==": lambda f, a: f == a,
        "=": lambda f, a: f == a,
        "eq": lambda f, a: f == a,
        "!=": lambda f, a: f != a,
        "<>": lambda f, a: f != a,
        "ne": lambda f, a: f != a,
        ">": lambda f, a: f > a,
        "gt": lambda f, a: f > a,
        "<": lambda f, a: f < a,
        "lt": lambda f, a: f < a,
        ">=": lambda f, a: f >= a,
        "ge": lambda f, a: f >= a,
        "<=": lambda f, a: f <= a,
        "le": lambda f, a: f <= a,
        "like": lambda f, a: f.like(a),
        "ilike": lambda f, a: f.ilike(a),
        "not_ilike": lambda f, a: ~f.ilike(a),
        "in": lambda f, a: f == any(a),
        "not_in": lambda f, a: ~f == any(a),
        "any": lambda f, a: f.any(a),
        "not_any": lambda f, a: f.not_(f.any(a)),
        "INTERSECTS": lambda f, a: Func(
            "st_intersects",
            f,
            Func("st_transform", Func("st_geomfromtext", a), Func("st_srid", f)),
        ),
        "DISJOINT": lambda f, a: Func(
            "st_disjoint", f, Func("st_transform", Func("st_geomfromtext", a), Func("st_srid", f))
        ),
        "CONTAINS": lambda f, a: Func(
            "st_contains", f, Func("st_transform", Func("st_geomfromtext", a), Func("st_srid", f))
        ),
        "WITHIN": lambda f, a: Func(
            "st_within", f, Func("st_transform", Func("st_geomfromtext", a), Func("st_srid", f))
        ),
        "TOUCHES": lambda f, a: Func(
            "st_touches", f, Func("st_transform", Func("st_geomfromtext", a), Func("st_srid", f))
        ),
        "CROSSES": lambda f, a: Func(
            "st_crosses",
            f,
            Func("st_transform", Func("st_geomfromtext", a), Func("st_srid", f)),
        ),
        "OVERLAPS": lambda f, a: Func(
            "st_overlaps",
            f,
            Func("st_transform", Func("st_geomfromtext", a), Func("st_srid", f)),
        ),
        "EQUALS": lambda f, a: Func(
            "st_equals",
            f,
            Func("st_transform", Func("st_geomfromtext", a), Func("st_srid", f)),
        ),
        "RELATE": lambda f, a, pattern: Func(
            "st_relate", f, Func("st_transform", Func("st_geomfromtext", a), Func("st_srid", f)), pattern
        ),
        "DWITHIN": lambda f, a, distance: Func(
            "st_dwithin", f, Func("st_transform", Func("st_geomfromtext", a), Func("st_srid", f)), distance
        ),
        "BEYOND": lambda f, a, distance: ~Func(
            "st_dwithin", f, Func("st_transform", Func("st_geomfromtext", a), Func("st_srid", f)), distance
        ),
        "+": lambda f, a: f + a,
        "-": lambda f, a: f - a,
        "*": lambda f, a: f * a,
        "/": lambda f, a: f / a,
    }

    def __init__(self, operator: str = None):
        """Init."""
        if not operator:
            operator = "=="

        if operator not in self.OPERATORS:
            raise Exception("Operator `{}` not valid.".format(operator))

        self.operator = operator
        self.function = self.OPERATORS[operator]
        self.arity = len(signature(self.function).parameters)
