import asyncio
from typing import List, Optional
from urllib.parse import urlparse
from uuid import UUID, uuid4

import structlog
from pydantic import ValidationError

from codegate.api import v1_models as apimodelsv1
from codegate.config import Config
from codegate.db import models as dbmodels
from codegate.db.connection import DbReader, DbRecorder
from codegate.muxing import models as mux_models
from codegate.providers.base import BaseProvider
from codegate.providers.registry import ProviderRegistry, get_provider_registry
from codegate.workspaces import crud as workspace_crud

logger = structlog.get_logger("codegate")


class ProviderNotFoundError(Exception):
    pass


class ProviderModelsNotFoundError(Exception):
    pass


class ProviderInvalidAuthConfigError(Exception):
    pass


class ProviderCrud:
    """The CRUD operations for the provider endpoint references within
    Codegate.

    This is meant to handle all the transformations in between the
    database and the API, as well as other sources of information. All
    operations should result in the API models being returned.
    """

    def __init__(self):
        self._db_reader = DbReader()
        self._db_writer = DbRecorder()
        self._ws_crud = workspace_crud.WorkspaceCrud()

    async def list_endpoints(self) -> List[apimodelsv1.ProviderEndpoint]:
        """List all the endpoints."""

        outendpoints = []
        dbendpoints = await self._db_reader.get_provider_endpoints()
        for dbendpoint in dbendpoints:
            outendpoints.append(apimodelsv1.ProviderEndpoint.from_db_model(dbendpoint))

        return outendpoints

    async def get_endpoint_by_id(self, id: UUID) -> Optional[apimodelsv1.ProviderEndpoint]:
        """Get an endpoint by ID."""

        dbendpoint = await self._db_reader.get_provider_endpoint_by_id(str(id))
        if dbendpoint is None:
            return None

        return apimodelsv1.ProviderEndpoint.from_db_model(dbendpoint)

    async def get_endpoint_by_name(self, name: str) -> Optional[apimodelsv1.ProviderEndpoint]:
        """Get an endpoint by name."""

        dbendpoint = await self._db_reader.get_provider_endpoint_by_name(name)
        if dbendpoint is None:
            raise ProviderNotFoundError(f'Provider "{name}" not found')

        return apimodelsv1.ProviderEndpoint.from_db_model(dbendpoint)

    async def _try_get_endpoint_by_name_and_type(
        self, name: str, type: Optional[str]
    ) -> Optional[apimodelsv1.ProviderEndpoint]:
        """
        Try to get an endpoint by name & type,
        falling back to a "best effort" match by type.
        """

        dbendpoint = await self._db_reader.try_get_provider_endpoint_by_name_and_type(name, type)
        if dbendpoint is None:
            raise ProviderNotFoundError(f'Provider "{name}" not found')

        return apimodelsv1.ProviderEndpoint.from_db_model(dbendpoint)

    async def add_provider_id_to_mux_rule(
        self, rule: mux_models.MuxRule
    ) -> mux_models.MuxRuleWithProviderId:
        endpoint = await self._try_get_endpoint_by_name_and_type(
            rule.provider_name, rule.provider_type
        )
        return mux_models.MuxRuleWithProviderId(
            model=rule.model,
            matcher=rule.matcher,
            matcher_type=rule.matcher_type,
            provider_name=endpoint.name,
            provider_type=endpoint.provider_type,
            provider_id=endpoint.id,
        )

    async def add_provider_ids_to_mux_rule_list(
        self, rules: List[mux_models.MuxRule]
    ) -> List[mux_models.MuxRuleWithProviderId]:
        rules_with_ids = []
        for rule in rules:
            rules_with_ids.append(await self.add_provider_id_to_mux_rule(rule))
        return rules_with_ids

    async def add_endpoint(
        self, endpoint: apimodelsv1.AddProviderEndpointRequest
    ) -> apimodelsv1.ProviderEndpoint:
        """Add an endpoint."""

        if not endpoint.endpoint:
            endpoint.endpoint = provider_default_endpoints(endpoint.provider_type)

        # If we STILL don't have an endpoint, we can't continue
        if not endpoint.endpoint:
            raise ValueError("No endpoint provided and no default found for provider type")

        dbend = endpoint.to_db_model()
        provider_registry = get_provider_registry()

        # We override the ID here, as we want to generate it.
        dbend.id = str(uuid4())

        prov = endpoint.get_from_registry(provider_registry)
        if prov is None:
            raise ValueError("Unknown provider type: {}".format(endpoint.provider_type))

        models = []
        if endpoint.auth_type == apimodelsv1.ProviderAuthType.api_key and not endpoint.api_key:
            raise ProviderInvalidAuthConfigError("API key must be provided for API auth type")
        if endpoint.auth_type != apimodelsv1.ProviderAuthType.passthrough:
            try:
                models = prov.models(endpoint=endpoint.endpoint, api_key=endpoint.api_key)
            except Exception as err:
                raise ProviderModelsNotFoundError(f"Unable to get models from provider: {err}")

        dbendpoint = await self._db_writer.add_provider_endpoint(dbend)

        await self._db_writer.push_provider_auth_material(
            dbmodels.ProviderAuthMaterial(
                provider_endpoint_id=dbendpoint.id,
                auth_type=endpoint.auth_type,
                auth_blob=endpoint.api_key if endpoint.api_key else "",
            )
        )

        for model in models:
            await self._db_writer.add_provider_model(
                dbmodels.ProviderModelIntermediate(
                    name=model,
                    provider_endpoint_id=dbendpoint.id,
                )
            )
        return apimodelsv1.ProviderEndpoint.from_db_model(dbendpoint)

    async def update_endpoint(
        self, endpoint: apimodelsv1.ProviderEndpoint
    ) -> apimodelsv1.ProviderEndpoint:
        """Update an endpoint."""

        if not endpoint.endpoint:
            endpoint.endpoint = provider_default_endpoints(endpoint.provider_type)

        # If we STILL don't have an endpoint, we can't continue
        if not endpoint.endpoint:
            raise ValueError("No endpoint provided and no default found for provider type")

        provider_registry = get_provider_registry()
        prov = endpoint.get_from_registry(provider_registry)
        if prov is None:
            raise ValueError("Unknown provider type: {}".format(endpoint.provider_type))

        founddbe = await self._db_reader.get_provider_endpoint_by_id(str(endpoint.id))
        if founddbe is None:
            raise ProviderNotFoundError("Provider not found")

        dbendpoint = await self._db_writer.update_provider_endpoint(endpoint.to_db_model())

        # If the auth type has not changed or no authentication is needed,
        # we can update the models
        if (
            founddbe.auth_type == endpoint.auth_type
            or endpoint.auth_type == apimodelsv1.ProviderAuthType.none
        ):
            try:
                authm = await self._db_reader.get_auth_material_by_provider_id(str(endpoint.id))

                models = await self._find_models_for_provider(
                    endpoint.endpoint, authm.auth_type, authm.auth_blob, prov
                )

                await self._update_models_for_provider(dbendpoint, models)

                # a model might have been deleted, let's repopulate the cache
                await self._ws_crud.repopulate_mux_cache()
            except Exception as err:
                # This is a non-fatal error. The endpoint might have changed
                # And the user will need to push a new API key anyway.
                logger.error(
                    "Unable to update models for provider",
                    provider=endpoint.name,
                    err=str(err),
                )

        return apimodelsv1.ProviderEndpoint.from_db_model(dbendpoint)

    async def configure_auth_material(
        self, provider_id: UUID, config: apimodelsv1.ConfigureAuthMaterial
    ):
        """Add an API key."""
        if config.auth_type == apimodelsv1.ProviderAuthType.api_key and not config.api_key:
            raise ProviderInvalidAuthConfigError("API key must be provided for API auth type")
        elif config.auth_type != apimodelsv1.ProviderAuthType.api_key and config.api_key:
            raise ProviderInvalidAuthConfigError("API key provided for non-API auth type")

        dbendpoint = await self._db_reader.get_provider_endpoint_by_id(str(provider_id))
        if dbendpoint is None:
            raise ProviderNotFoundError("Provider not found")

        endpoint = apimodelsv1.ProviderEndpoint.from_db_model(dbendpoint)
        endpoint.auth_type = config.auth_type
        provider_registry = get_provider_registry()
        prov = endpoint.get_from_registry(provider_registry)

        models = await self._find_models_for_provider(
            endpoint.endpoint, config.auth_type, config.api_key, prov
        )

        await self._db_writer.push_provider_auth_material(
            dbmodels.ProviderAuthMaterial(
                provider_endpoint_id=dbendpoint.id,
                auth_type=config.auth_type,
                auth_blob=config.api_key if config.api_key else "",
            )
        )

        await self._update_models_for_provider(dbendpoint, models)

        # a model might have been deleted, let's repopulate the cache
        await self._ws_crud.repopulate_mux_cache()

    async def _find_models_for_provider(
        self,
        endpoint: str,
        auth_type: apimodelsv1.ProviderAuthType,
        api_key: str,
        prov: BaseProvider,
    ) -> List[str]:
        if auth_type != apimodelsv1.ProviderAuthType.passthrough:
            try:
                return prov.models(endpoint=endpoint, api_key=api_key)
            except Exception as err:
                raise ProviderModelsNotFoundError(f"Unable to get models from provider: {err}")
        return []

    async def _update_models_for_provider(
        self,
        dbendpoint: dbmodels.ProviderEndpoint,
        found_models: List[str],
    ) -> None:
        models_set = set(found_models)

        # Get the models from the provider
        models_in_db = await self._db_reader.get_provider_models_by_provider_id(str(dbendpoint.id))

        models_in_db_set = set(model.name for model in models_in_db)

        # Add the models that are in the provider but not in the DB
        for model in models_set - models_in_db_set:
            await self._db_writer.add_provider_model(
                dbmodels.ProviderModelIntermediate(
                    name=model,
                    provider_endpoint_id=dbendpoint.id,
                )
            )

        # Remove the models that are in the DB but not in the provider
        for model in models_in_db_set - models_set:
            await self._db_writer.delete_provider_model(
                dbendpoint.id,
                model,
            )

    async def delete_endpoint(self, provider_id: UUID):
        """Delete an endpoint."""

        dbendpoint = await self._db_reader.get_provider_endpoint_by_id(str(provider_id))
        if dbendpoint is None:
            raise ProviderNotFoundError("Provider not found")

        await self._db_writer.delete_provider_endpoint(dbendpoint)

        await self._ws_crud.repopulate_mux_cache()

    async def models_by_provider(self, provider_id: UUID) -> List[apimodelsv1.ModelByProvider]:
        """Get the models by provider."""

        # First we try to get the provider
        dbendpoint = await self._db_reader.get_provider_endpoint_by_id(str(provider_id))
        if dbendpoint is None:
            raise ProviderNotFoundError("Provider not found")

        outmodels = []
        dbmodels = await self._db_reader.get_provider_models_by_provider_id(str(provider_id))
        for dbmodel in dbmodels:
            outmodels.append(
                apimodelsv1.ModelByProvider(
                    name=dbmodel.name,
                    provider_name=dbendpoint.name,
                    provider_type=dbendpoint.provider_type,
                )
            )

        return outmodels

    async def get_all_models(self) -> List[apimodelsv1.ModelByProvider]:
        """Get all the models."""

        outmodels = []
        dbmodels = await self._db_reader.get_all_provider_models()
        for dbmodel in dbmodels:
            ename = dbmodel.provider_endpoint_name if dbmodel.provider_endpoint_name else ""
            outmodels.append(
                apimodelsv1.ModelByProvider(
                    name=dbmodel.name,
                    provider_name=ename,
                    provider_type=dbmodel.provider_endpoint_type,
                )
            )

        return outmodels


async def initialize_provider_endpoints(preg: ProviderRegistry):
    db_writer = DbRecorder()
    db_reader = DbReader()
    config = Config.get_config()
    if config is None:
        provided_urls = {}
    else:
        provided_urls = config.provider_urls

    for provider_name, provider_url in provided_urls.items():
        provend = __provider_endpoint_from_cfg(provider_name, provider_url)
        if provend is None:
            continue

        # Check if the provider is already in the db
        dbprovend = await db_reader.get_provider_endpoint_by_name(provend.name)
        if dbprovend is not None:
            logger.debug(
                "Provider already in DB. skipping",
                provider=provend.name,
                endpoint=provend.endpoint,
            )
            continue

        pimpl = provend.get_from_registry(preg)
        if pimpl is None:
            logger.warning(
                "Provider not found in registry",
                provider=provend.name,
                endpoint=provend.endpoint,
            )
            continue
        await try_initialize_provider_endpoints(provend, pimpl, db_writer)

    provcrud = ProviderCrud()

    endpoints = await provcrud.list_endpoints()
    for endpoint in endpoints:
        dbprovend = await db_reader.get_provider_endpoint_by_name(endpoint.name)
        pimpl = endpoint.get_from_registry(preg)
        if pimpl is None:
            logger.warning(
                "Provider not found in registry",
                provider=endpoint.name,
                endpoint=endpoint.endpoint,
            )
            continue
        await try_update_to_provider(provcrud, pimpl, dbprovend)


async def try_initialize_provider_endpoints(
    provend: apimodelsv1.ProviderEndpoint,
    pimpl: BaseProvider,
    db_writer: DbRecorder,
):
    try:
        models = pimpl.models()

        # If we were able to get the models, we don't need auth
        provend.auth_type = apimodelsv1.ProviderAuthType.none
    except Exception as err:
        logger.debug(
            "Unable to get models from provider",
            provider=provend.name,
            err=str(err),
        )
        return

    logger.info(
        "initializing provider to DB",
        provider=provend.name,
        endpoint=provend.endpoint,
        models=models,
    )
    # We only try to add the provider if we have models
    await db_writer.add_provider_endpoint(provend.to_db_model())

    tasks = set()
    for model in models:
        tasks.add(
            db_writer.add_provider_model(
                dbmodels.ProviderModel(
                    provider_endpoint_id=provend.id,
                    name=model,
                    provider_endpoint_type=provend.provider_type,
                    provider_endpoint_name=provend.name,
                )
            )
        )

    await asyncio.gather(*tasks)


async def try_update_to_provider(
    provcrud: ProviderCrud, prov: BaseProvider, dbprovend: dbmodels.ProviderEndpoint
):
    authm = await provcrud._db_reader.get_auth_material_by_provider_id(str(dbprovend.id))

    try:
        models = await provcrud._find_models_for_provider(
            dbprovend.endpoint, authm.auth_type, authm.auth_blob, prov
        )
    except Exception as err:
        logger.info(
            "Unable to get models from provider. Skipping",
            provider=dbprovend.name,
            err=str(err),
        )
        return

    await provcrud._update_models_for_provider(dbprovend, models)

    # a model might have been deleted, let's repopulate the cache
    await provcrud._ws_crud.repopulate_mux_cache()


def __provider_endpoint_from_cfg(
    provider_name: str, provider_url: str
) -> Optional[apimodelsv1.ProviderEndpoint]:
    """Create a provider endpoint from the config entry."""

    try:
        _ = urlparse(provider_url)
    except Exception:
        logger.warning(
            "Invalid provider URL", provider_name=provider_name, provider_url=provider_url
        )
        return None

    try:
        return apimodelsv1.ProviderEndpoint(
            id=str(uuid4()),
            name=provider_name,
            endpoint=provider_url,
            description=("Endpoint for the {} provided via the CodeGate configuration.").format(
                provider_name
            ),
            provider_type=provider_overrides(provider_name),
            auth_type=apimodelsv1.ProviderAuthType.passthrough,
        )
    except ValidationError as err:
        logger.warning(
            "Invalid provider name",
            provider_name=provider_name,
            provider_url=provider_url,
            err=str(err),
        )
        return None


def provider_default_endpoints(provider_type: str) -> str:
    # TODO: These providers default endpoints should come from config.py
    defaults = {
        "openai": "https://api.openai.com",
        "anthropic": "https://api.anthropic.com",
    }

    # If we have a default, we return it
    # Otherwise, we return an empty string
    return defaults.get(provider_type, "")


def provider_overrides(provider_type: str) -> str:
    overrides = {
        "lm_studio": "openai",
    }

    # If we have an override, we return it
    # Otherwise, we return the type
    return overrides.get(provider_type, provider_type)
