import urllib
import traceback

from typing import Dict, Any, Union, List
from datetime import datetime

from fastapi.encoders import jsonable_encoder
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from optimade.models import (
    ToplevelLinks,
    ResponseMeta,
    ResponseMetaQuery,
    Provider,
    Error,
    ErrorResponse,
    EntryResource,
    StructureResource,
    ReferenceResource,
    EntryResponseMany,
    EntryResponseOne,
)

from .config import CONFIG
from .deps import EntryListingQueryParams, SingleEntryQueryParams
from .entry_collections import EntryCollection


def meta_values(
    url: str,
    data_returned: int,
    data_available: int,
    more_data_available: bool,
    **kwargs,
) -> ResponseMeta:
    """Helper to initialize the meta values"""
    parse_result = urllib.parse.urlparse(url)
    provider = CONFIG.provider.copy()
    provider["prefix"] = provider["prefix"][1:-1]  # Remove surrounding `_`
    return ResponseMeta(
        query=ResponseMetaQuery(
            representation=f"{parse_result.path}?{parse_result.query}"
        ),
        api_version="v0.10",
        time_stamp=datetime.utcnow(),
        data_returned=data_returned,
        more_data_available=more_data_available,
        provider=Provider(**provider),
        data_available=data_available,
        **kwargs,
    )


def general_exception(
    request: Request, exc: Exception, **kwargs: Dict[str, Any]
) -> JSONResponse:
    tb = "".join(
        traceback.format_exception(etype=type(exc), value=exc, tb=exc.__traceback__)
    )
    print(tb)

    try:
        status_code = exc.status_code
    except AttributeError:
        status_code = kwargs.get("status_code", 500)

    detail = getattr(exc, "detail", str(exc))

    errors = kwargs.get("errors", None)
    if not errors:
        errors = [
            Error(detail=detail, status=status_code, title=str(exc.__class__.__name__))
        ]

    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(
            ErrorResponse(
                meta=meta_values(
                    url=str(request.url),
                    data_returned=0,
                    data_available=0,
                    more_data_available=False,
                    **{CONFIG.provider["prefix"] + "traceback": tb},
                ),
                errors=errors,
            ),
            skip_defaults=True,
        ),
    )


def handle_response_fields(
    results: Union[List[EntryResource], EntryResource], fields: set
) -> dict:
    if not isinstance(results, list):
        results = [results]
    non_attribute_fields = {"id", "type"}
    top_level = {_ for _ in non_attribute_fields if _ in fields}
    attribute_level = fields - non_attribute_fields
    new_results = []
    while results:
        entry = results.pop(0)
        new_entry = entry.dict(exclude=top_level, skip_defaults=True)
        for field in attribute_level:
            if field in new_entry["attributes"]:
                del new_entry["attributes"][field]
        if not new_entry["attributes"]:
            del new_entry["attributes"]
        new_results.append(new_entry)
    return new_results


def get_entries(
    collection: EntryCollection,
    response: EntryResponseMany,
    request: Request,
    params: EntryListingQueryParams,
) -> EntryResponseMany:
    """Generalized /{entry} endpoint getter"""
    results, more_data_available, data_available, fields = collection.find(params)

    if more_data_available:
        parse_result = urllib.parse.urlparse(str(request.url))
        query = urllib.parse.parse_qs(parse_result.query)
        query["page_offset"] = int(query.get("page_offset", [0])[0]) + len(results)
        urlencoded = urllib.parse.urlencode(query, doseq=True)
        links = ToplevelLinks(
            next=f"{parse_result.scheme}://{parse_result.netloc}{parse_result.path}?{urlencoded}"
        )
    else:
        links = ToplevelLinks(next=None)

    if fields:
        results = handle_response_fields(results, fields)

    return response(
        links=links,
        data=results,
        meta=meta_values(
            str(request.url), len(results), data_available, more_data_available
        ),
    )


def get_single_entry(
    collection: EntryCollection,
    entry_id: str,
    response: EntryResponseOne,
    request: Request,
    params: SingleEntryQueryParams,
) -> EntryResponseOne:
    params.filter = f'id="{entry_id}"'
    results, more_data_available, data_available, fields = collection.find(params)

    if more_data_available:
        raise StarletteHTTPException(
            status_code=500,
            detail=f"more_data_available MUST be False for single entry response, however it is {more_data_available}",
        )

    links = ToplevelLinks(next=None)

    if fields and results is not None:
        results = handle_response_fields(results, fields)[0]

    data_returned = 1 if results else 0

    return response(
        links=links,
        data=results,
        meta=meta_values(
            str(request.url), data_returned, data_available, more_data_available
        ),
    )


def retrieve_queryable_properties(schema: dict, queryable_properties: list) -> dict:
    properties = {}
    for name, value in schema["properties"].items():
        if name in queryable_properties:
            if "$ref" in value:
                path = value["$ref"].split("/")[1:]
                sub_schema = schema.copy()
                while path:
                    next_key = path.pop(0)
                    sub_schema = sub_schema[next_key]
                sub_queryable_properties = sub_schema["properties"].keys()
                properties.update(
                    retrieve_queryable_properties(sub_schema, sub_queryable_properties)
                )
            else:
                properties[name] = {"description": value.get("description", "")}
                if "unit" in value:
                    properties[name]["unit"] = value["unit"]
    return properties


ENTRY_INFO_SCHEMAS = {
    "structures": StructureResource.schema,
    "references": ReferenceResource.schema,
}