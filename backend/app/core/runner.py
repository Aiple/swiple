import json
import datetime
import uuid
from typing import Literal

from great_expectations.core import ExpectationSuite, ExpectationConfiguration
from great_expectations.core.batch import RuntimeBatchRequest, BatchRequest
from great_expectations.data_context import BaseDataContext
from great_expectations.data_context.types.base import DataContextConfig, AnonymizedUsageStatisticsConfig
from great_expectations.data_context.types.base import InMemoryStoreBackendDefaults
from great_expectations.profile.user_configurable_profiler import UserConfigurableProfiler
from opensearchpy import OpenSearch
from pandas import isnull


from app import constants as c
from app import utils
from app.core.actions import action_dispatcher
from app.core.expectations import supported_unsupported_expectations
from app.db.client import client as os_client
from app.models.datasource import Engine
from app.models.validation import Validation
from app.repositories.dataset import DatasetRepository
from app.repositories.datasource import DatasourceRepository
from app.repositories.expectation import ExpectationRepository
from app.settings import settings


class Runner:
    def __init__(self, datasource, batch, meta, dataset_id=None, datasource_id=None, expectations=None,
                 identifiers=None, excluded_expectations=[]):
        self.identifiers = identifiers
        self.datasource = datasource
        self.batch = batch
        self.expectations = expectations
        self.meta = meta
        self.datasource_id = datasource_id
        self.dataset_id = dataset_id
        self.excluded_expectations = excluded_expectations

    def profile(self):
        assert self.datasource_id is not None, 'Require "datasource_id" when profiling.'
        assert self.dataset_id is not None, 'Require "dataset_id" when profiling.'

        data_context_config = self.get_data_context_config()
        context = BaseDataContext(project_config=data_context_config)
        suite: ExpectationSuite = context.create_expectation_suite("default", overwrite_existing=True)

        batch_request = self.get_batch_request(is_profile=True)

        validator = context.get_validator(
            batch_request=batch_request,
            expectation_suite=suite,
        )

        profiler = UserConfigurableProfiler(
            validator,
            excluded_expectations=self.excluded_expectations,
            value_set_threshold="few",
        )
        expectations = profiler.build_suite().to_json_dict()['expectations']

        for expectation in expectations:
            expectation["kwargs"].update({"result_format": "SUMMARY", "include_config": True, "catch_exceptions": True})

            if expectation["kwargs"].get("mostly"):
                expectation["kwargs"]["objective"] = expectation["kwargs"].pop("mostly")

            expectation["kwargs"] = json.dumps(expectation["kwargs"])
            expectation["enabled"] = False
            expectation["suggested"] = True
            expectation["datasource_id"] = self.datasource_id
            expectation["dataset_id"] = self.dataset_id
            expectation["create_date"] = utils.current_time()
            expectation["modified_date"] = utils.current_time()

        return expectations

    def sample(self):
        data_context_config = self.get_data_context_config()
        context = BaseDataContext(project_config=data_context_config)

        batch_request = self.get_batch_request()

        suite: ExpectationSuite = context.create_expectation_suite("default", overwrite_existing=True)

        try:
            validator = context.get_validator(
                batch_request=batch_request, expectation_suite=suite,
            )
            head = validator.head()
        except KeyError as ex:
            if self.batch.runtime_parameters:
                return {"exception": f"Syntax error in query."}
            else:
                print(str(ex))
                return {"exception": f"{self.batch.dataset_name} is not recognized."}

        rows = head.to_dict(orient='records')
        columns = head.columns

        for record in rows:
            for column in columns:
                if isinstance(record[column], datetime.datetime):
                    record[column] = record[column].__str__()

                if isnull(record[column]):
                    record[column] = None
        return {'columns': list(columns), 'rows': rows}

    def validate(self) -> Validation:
        data_context_config = self.get_data_context_config()
        context = BaseDataContext(project_config=data_context_config)

        suite: ExpectationSuite = context.create_expectation_suite("default", overwrite_existing=True)

        for expectation in self.expectations:
            if self.batch.runtime_parameters:
                expectation_meta = {**self.meta, **self.batch.runtime_parameters.dict(by_alias=True)}
            else:
                expectation_meta = {**self.meta}

            if expectation.get("meta"):
                expectation_meta = expectation.get("meta")

            if expectation["kwargs"].get("objective"):
                expectation["kwargs"]["mostly"] = expectation["kwargs"].pop("objective")

            expectation_configuration = ExpectationConfiguration(
                expectation_type=expectation["expectation_type"],
                kwargs=expectation["kwargs"],
                meta=expectation_meta,
            )

            suite.add_expectation(expectation_configuration=expectation_configuration)

        batch_request = self.get_batch_request()
        validator = context.get_validator(
            batch_request=batch_request, expectation_suite=suite,
        )

        validation = validator.validate().to_json_dict()
        validation["meta"]["run_id"]["run_time"] = utils.remove_t_from_date_string(
            validation["meta"]["run_id"]["run_time"])
        validation["meta"]["run_id"]["run_name"] = str(uuid.uuid4())
        validation["meta"].update(self.identifiers)

        for result in validation["results"]:
            # GE "mostly" is synonymous for Swiple "objective"
            if result["expectation_config"]["kwargs"].get("mostly"):
                result["expectation_config"]["kwargs"]["objective"] = result["expectation_config"]["kwargs"].pop("mostly")

            if isinstance(result["result"].get("observed_value"), list):
                result["result"]["observed_value_list"] = result["result"].pop("observed_value")

            utils.list_to_string_mapper(result)
            result["expectation_id"] = result["expectation_config"]["meta"].pop("expectation_id")

        action_dispatcher.dispatch(
            resource_key=self.identifiers["dataset_id"],
            action_type="validation",
            action_status=self._get_status(validation["success"]),
            validation=validation,
        )

        return Validation(**validation)

    def get_data_context_config(self):
        connection_string = self._get_connection_string()

        context = DataContextConfig(
            datasources={
                self.datasource.datasource_name: {
                    "class_name": "Datasource",
                    "module_name": "great_expectations.datasource",
                    "execution_engine": {
                        "class_name": "SqlAlchemyExecutionEngine",
                        "connection_string": connection_string,
                    },
                    "data_connectors": {
                        "default_runtime_data_connector": {
                            "class_name": "RuntimeDataConnector",

                            # Is there a use-case where this is needed?
                            # If we require users to add all details for runs in the app then
                            # using SDK, it pulls values into SDK? This would require batch_identifiers
                            # for datasources like spark/airflow TODO do this when spark/airflow is added
                            #
                            # Alternative is to let users push to ES runs without
                            # values in app. (Don't like the sound of that...)
                            "batch_identifiers": [
                                self.batch.dataset_name
                            ],
                        },
                        "default_inferred_data_connector_name": {
                            "class_name": "InferredAssetSqlDataConnector",
                            "include_schema_name": True,
                        },
                    }
                }
            },
            store_backend_defaults=InMemoryStoreBackendDefaults(),
            concurrency={
                "enabled": True,
            },
            anonymous_usage_statistics=AnonymizedUsageStatisticsConfig(enabled=False)
        )
        return context

    def get_batch_request(self, is_profile=True):
        if self.batch.runtime_parameters:
            batch_spec_passthrough = None
            if is_profile:
                # Bug when profiling. Requires physical/temp table to get column types.
                # set back to False when  https://github.com/great-expectations/great_expectations/issues/4832
                # is fixed
                batch_spec_passthrough = {"create_temp_table": True}

            runtime_parameters = self.batch.runtime_parameters.dict(by_alias=True)
            return RuntimeBatchRequest(
                datasource_name=self.datasource.datasource_name,
                data_connector_name="default_runtime_data_connector",
                data_asset_name=self.batch.dataset_name,
                runtime_parameters=runtime_parameters,
                batch_identifiers={self.batch.dataset_name: self.batch.dataset_name},
                batch_spec_passthrough=batch_spec_passthrough,
            )
        else:
            return BatchRequest(
                datasource_name=self.datasource.datasource_name,
                data_connector_name="default_inferred_data_connector_name",
                data_asset_name=self.batch.dataset_name,
                batch_spec_passthrough={"create_temp_table": False},
            )

    def _get_connection_string(self):
        # Snowflake SQLAlchemy connector requires the schema in the connection string in order to create TEMP tables.
        if self.datasource.engine == Engine.SNOWFLAKE and self.batch.runtime_parameters:
            schema = self.batch.runtime_parameters.schema_name
            connection_string = self.datasource.connection_string(schema)
        # BigQuery SQLAlchemy connector requires the dataset_id/schema in connection string
        elif self.datasource.engine == Engine.BIGQUERY and self.batch.runtime_parameters:
            schema = self.batch.runtime_parameters.schema_name
            connection_string = self.datasource.connection_string(schema)
        elif self.datasource.engine == Engine.BIGQUERY and not self.batch.runtime_parameters:
            schema, _ = self.batch.dataset_name.split(".")
            connection_string = self.datasource.connection_string(schema)
        else:
            connection_string = self.datasource.connection_string()

        return connection_string

    @staticmethod
    def _get_status(success: bool) -> Literal["success", "failure"]:
        action_status: Literal["failure"] = "failure"
        if success:
            action_status: Literal["success"] = "success"
        return action_status


def run_dataset_validation(dataset_id: str, client: OpenSearch = os_client):
    dataset = DatasetRepository(client).get(dataset_id)
    datasource = DatasourceRepository(client).get(dataset.datasource_id)
    expectations = ExpectationRepository(client).query_by_filter(dataset_id=dataset.key, enabled=True)

    identifiers = {
        "datasource_id": datasource.key,
        "dataset_id": dataset.key,
    }

    meta = {
        **datasource.expectation_meta(),
        "dataset_name": dataset.dataset_name,
    }

    runner_expectations = []
    for expectation in expectations:
        runner_expectation = expectation.dict()
        runner_expectation["meta"] = {"expectation_id": expectation.key}
        runner_expectations.append(runner_expectation)

    validation = Runner(
        datasource=datasource,
        batch=dataset,
        meta=meta,
        expectations=runner_expectations,
        identifiers=identifiers,
    ).validate()

    client.index(
        index=settings.VALIDATION_INDEX,
        id=str(uuid.uuid4()),
        body=validation.dict(),
        refresh="wait_for",
    )

    return validation


def create_dataset_suggestions(dataset_id: str, client: OpenSearch = os_client):
    dataset = DatasetRepository(client).get(dataset_id)
    datasource = DatasourceRepository(client).get(dataset.datasource_id)
    
    identifiers = {
        "datasource_id": datasource.key,
        "dataset_id": dataset.key,
    }

    meta = {
        **datasource.expectation_meta(),
        "dataset_name": dataset.dataset_name,
    }

    excluded_expectations = supported_unsupported_expectations()["unsupported_expectations"]
    excluded_expectations.append(c.EXPECT_COLUMN_VALUES_TO_BE_BETWEEN)

    results = Runner(
        datasource=datasource,
        batch=dataset,
        meta=meta,
        identifiers=identifiers,
        datasource_id=dataset.datasource_id,
        dataset_id=dataset.key,
        excluded_expectations=excluded_expectations,
    ).profile()

    return results
