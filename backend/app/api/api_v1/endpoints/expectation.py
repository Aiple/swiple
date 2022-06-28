from typing import Optional
from fastapi import APIRouter, HTTPException, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from app.models.expectation import Expectation
from app.core.expectations import supported_unsupported_expectations
from app import utils
from app.db.client import client
from app.config.settings import settings
from opensearchpy import NotFoundError, RequestError
from app.models import expectation as exp
from copy import deepcopy
from pydantic.error_wrappers import ValidationError
from app.utils import json_schema_to_single_doc
from app.api.api_v1.endpoints import validation
from fastapi.param_functions import Depends
from app.core.users import current_active_user
import json
import uuid


router = APIRouter(
    dependencies=[Depends(current_active_user)]
)


@router.get("/json_schema")
def get_json_schema():
    expectations = []
    for expectation in exp.type_map.values():
        json_schema = json_schema_to_single_doc(expectation.schema())
        expectations.append(json_schema)

    return JSONResponse(status_code=status.HTTP_200_OK, content=expectations)


@router.get("/supported_expectations")
def get_supported_expectations():
    content = supported_unsupported_expectations()
    return JSONResponse(status_code=status.HTTP_200_OK, content=content)


@router.get("")
def get_expectations(
        datasource_id: Optional[str] = None,
        dataset_id: Optional[str] = None,
        include_history: Optional[bool] = False,
        asc: Optional[bool] = True,
):
    # TODO implement scrolling
    direction = "asc" if asc else "desc"
    sort_by_key: str = "expectation_type"

    query = {"query": {"match": {}}, "sort": [{sort_by_key: direction}]}

    if datasource_id is None and dataset_id is None:
        query = {"query": {"match_all": {}}, "sort": [{sort_by_key: direction}]}
    else:
        if datasource_id is not None:
            query["query"]["match"]["datasource_id"] = datasource_id

        if dataset_id is not None:
            query["query"]["match"]["dataset_id"] = dataset_id

    try:
        if include_history:
            body = [
                {"index": settings.EXPECTATION_INDEX},
                {**{"size": 1000}, **query},
                {"index": settings.VALIDATION_INDEX},
                validation.validations_query_body(datasource_id, dataset_id),
            ]

            results = client.msearch(body=body)

            expectations = results["responses"][0]["hits"]["hits"]
            validations = results["responses"][1]["hits"]["hits"]

            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=zip_expectations_and_validations(expectations, validations)
            )

        results = client.search(
            index=settings.EXPECTATION_INDEX,
            size=1000,
            body=query
        )["hits"]["hits"]
    except RequestError as ex:
        print(ex)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid sort_by_key"
        )

    result_response = []
    for result in results:
        source = result["_source"]
        expectation_type = source["expectation_type"]
        source["kwargs"] = json.loads(source["kwargs"])
        expectation = exp.type_map[expectation_type](**source)
        source["documentation"] = expectation.documentation()
        result_response.append(
            dict(**{"key": result["_id"]}, **source)
        )
    return JSONResponse(status_code=status.HTTP_200_OK, content=result_response)


@router.get("/{key}")
def get_expectation(key: str):
    doc = client.get(
        index=settings.EXPECTATION_INDEX,
        id=key
    )["_source"]

    doc["key"] = key

    return JSONResponse(status_code=status.HTTP_200_OK, content=doc)


@router.post("")
def create_expectation(expectation: Expectation):
    # TODO validation, don't allow datasource_id, dataset_id, expectation_id in "meta" field. We add this fields to meta in Runner.run
    try:
        expectation.create_date = utils.current_time()
        expectation.modified_date = utils.current_time()
        expectation: dict = exp.type_map[expectation.expectation_type](**expectation.dict(exclude_none=True)).dict(exclude_none=True)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"expectation '{expectation.expectation_type}' has not been implemented"
        )
    except ValidationError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=jsonable_encoder({"detail": exc.errors(), "body": expectation}),
        )

    _resource_exists(
        expectation["datasource_id"],
        settings.DATASOURCE_INDEX,
        "datasource"
    )

    dataset = _resource_exists(
        expectation["dataset_id"],
        settings.DATASET_INDEX,
        "dataset_id"
    )["_source"]

    if dataset.get("datasource_id") != expectation["datasource_id"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="expectation datasource_id does not match dataset datasource_id"
        )

    expectation_copy = deepcopy(expectation)
    expectation_copy["kwargs"] = json.dumps(expectation_copy["kwargs"])

    response = client.index(
        index=settings.EXPECTATION_INDEX,
        id=str(uuid.uuid4()),
        body=expectation_copy,
        refresh="wait_for",
    )
    expectation_copy["key"] = response["_id"]
    expectation_copy["kwargs"] = json.loads(expectation_copy["kwargs"])
    return JSONResponse(status_code=status.HTTP_200_OK, content=expectation_copy)


@router.put("/{key}")
def update_expectation(expectation: Expectation, key: str):
    try:
        expectation.modified_date = utils.current_time()
        expectation = exp.type_map[expectation.expectation_type](**expectation.dict(exclude_none=True)).dict(exclude_none=True)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"expectation '{expectation.expectation_type}' has not been implemented"
        )
    except ValidationError as exc:
        print(exc)
        print(expectation)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=jsonable_encoder({"detail": exc.errors(), "body": expectation}),
        )

    try:
        original_expectation = client.get(
            index=settings.EXPECTATION_INDEX,
            id=key
        )["_source"]

        expectation['create_date'] = original_expectation['create_date']
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"expectation with key '{key}' does not exist"
        )

    expectation_copy = deepcopy(expectation)
    expectation_copy["kwargs"] = json.dumps(expectation_copy["kwargs"])

    if original_expectation == expectation_copy:
        expectation["key"] = key
        return JSONResponse(status_code=status.HTTP_200_OK, content=expectation)

    if original_expectation["datasource_id"] != expectation_copy["datasource_id"]:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content="updates to expectation datasource_id are not supported",
        )

    if original_expectation["dataset_id"] != expectation_copy["dataset_id"]:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content="updates to expectation dataset_id are not supported",
        )

    # This allows the user to edit an existing
    # expectations expectation_type without having to
    # delete it and create a new one. We handle it for them.
    # We want to delete the existing expectation in-case we
    # decide to run aggregations on validations that have been
    # run. We can't have an expectation with the same id but
    # with different expectation types
    if original_expectation["expectation_type"] != expectation_copy["expectation_type"]:
        response = client.index(
            index=settings.EXPECTATION_INDEX,
            id=str(uuid.uuid4()),
            body=expectation_copy,
            refresh="wait_for",
        )
        expectation["key"] = response["_id"]

        client.delete(index=settings.EXPECTATION_INDEX, id=key, refresh="wait_for")
        client.delete_by_query(
            index=settings.VALIDATION_INDEX,
            body={"query": {"match": {"expectation_id": key}}}
        )
        return JSONResponse(status_code=status.HTTP_200_OK, content=expectation)

    response = client.update(
        index=settings.EXPECTATION_INDEX,
        id=key,
        body={"doc": expectation_copy},
        refresh="wait_for",
    )
    expectation["key"] = response["_id"]
    return JSONResponse(status_code=status.HTTP_200_OK, content=expectation)


@router.delete("/{key}")
def delete_expectation(key: str):
    try:
        client.delete_by_query(
            index=settings.VALIDATION_INDEX,
            body={"query": {"match": {"expectation_id": key}}}
        )
        client.delete(index=settings.EXPECTATION_INDEX, id=key, refresh="wait_for", )
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"expectation with key '{key}' does not exist"
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content="expectation deleted"
    )


def _resource_exists(key: str, index: str, resource_type: str):
    try:
        return client.get(
            index=index,
            id=key
        )
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{resource_type} with id '{key}' does not exist"
        )


def zip_expectations_and_validations(expectations, validations):
    expectations_as_dict = {}

    for expectation in expectations:
        source = expectation["_source"]
        expectation_type = source["expectation_type"]
        source["kwargs"] = json.loads(source["kwargs"])
        expectation_obj = exp.type_map[expectation_type](**source)
        source["documentation"] = expectation_obj.documentation()
        source["result_type"] = expectation_obj.result_type
        source["validations"] = []
        expectations_as_dict[expectation["_id"]] = dict(**{"key": expectation["_id"]}, **source)

    for v in validations:
        source = v["_source"]
        source["run_date"] = utils.string_to_military_time(source["run_date"])
        expectations_as_dict[source["expectation_id"]]["validations"].append(source)

    return list(expectations_as_dict.values())