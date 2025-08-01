#  Copyright (c) ZenML GmbH 2023. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at:
#
#       https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
#  or implied. See the License for the specific language governing
#  permissions and limitations under the License.
"""CLI functionality to interact with Model Control Plane."""

from typing import Any, Dict, List, Optional

import click

from zenml.cli import utils as cli_utils
from zenml.cli.cli import TagGroup, cli
from zenml.cli.utils import (
    enhanced_list_options,
    format_date_for_table,
    prepare_list_data,
)
from zenml.client import Client
from zenml.console import console
from zenml.enums import CliCategories, ModelStages
from zenml.exceptions import EntityExistsError
from zenml.logger import get_logger
from zenml.models import (
    ModelFilter,
    ModelResponse,
    ModelVersionArtifactFilter,
    ModelVersionFilter,
    ModelVersionPipelineRunFilter,
    ModelVersionResponse,
)
from zenml.utils.dict_utils import remove_none_values

logger = get_logger(__name__)


def _model_to_print(model: ModelResponse) -> Dict[str, Any]:
    """Convert a model response to a dictionary suitable for table display.

    For table output, keep it ultra-compact with only core information.
    Full details including tags and description are available in JSON/YAML
    output formats.

    Args:
        model: Model response object

    Returns:
        Dictionary containing formatted model data for table display
    """
    return {
        "name": model.name,
        "latest_version": model.latest_version_name or "-",
        "updated": format_date_for_table(model.updated),
    }


def _model_to_print_full(model: ModelResponse) -> Dict[str, Any]:
    """Convert model response to complete dictionary for JSON/YAML.

    Args:
        model: Model response object

    Returns:
        Complete dictionary containing all model data
    """
    return model.model_dump(mode="json")


def _model_version_to_print(
    model_version: ModelVersionResponse,
) -> Dict[str, Any]:
    """Convert model version response to dictionary for table display.

    For table output, keep it compact with essential information and visual
    stage indicators. Full details including ID, description, and run_metadata
    are available in JSON/YAML output formats.

    Args:
        model_version: Model version response object

    Returns:
        Dictionary containing formatted model version data for table display
    """
    return {
        "model": model_version.model.name,
        "version": model_version.name or f"#{model_version.number}",
        "stage": model_version.stage or "",
        "tags": [t.name for t in model_version.tags]
        if model_version.tags
        else [],
        "updated": format_date_for_table(model_version.updated),
        # Internal field for stage formatting (removed from non-table outputs)
        "__stage_value__": model_version.stage or "",
    }


def _model_version_to_print_full(
    model_version: ModelVersionResponse,
) -> Dict[str, Any]:
    """Convert model version response to complete dictionary for JSON/YAML.

    Args:
        model_version: Model version response object

    Returns:
        Complete dictionary containing all model version data
    """
    return model_version.model_dump(mode="json")


@cli.group(cls=TagGroup, tag=CliCategories.MODEL_CONTROL_PLANE)
def model() -> None:
    """Interact with models and model versions in the Model Control Plane."""


@enhanced_list_options(ModelFilter)
@model.command("list", help="List models with filter.")
def list_models(**kwargs: Any) -> None:
    """List models with filter in the Model Control Plane.

    Args:
        **kwargs: Keyword arguments to filter models.
    """
    # Extract table options from kwargs
    table_kwargs = cli_utils.extract_table_options(kwargs)

    with console.status("Listing models..."):
        models = Client().list_models(**kwargs)

    if not models:
        cli_utils.declare("No models found.")
        return

    # Prepare data based on output format
    output_format = (
        table_kwargs.get("output") or cli_utils.get_default_output_format()
    )
    model_data = []

    # Use centralized data preparation
    model_data = prepare_list_data(
        models, output_format, _model_to_print, _model_to_print_full
    )

    # Handle table output with enhanced system and pagination
    cli_utils.handle_table_output(model_data, page=models, **table_kwargs)


@model.command("register", help="Register a new model.")
@click.option(
    "--name",
    "-n",
    help="The name of the model.",
    type=str,
    required=True,
)
@click.option(
    "--license",
    "-l",
    help="The license under which the model is created.",
    type=str,
    required=False,
)
@click.option(
    "--description",
    "-d",
    help="The description of the model.",
    type=str,
    required=False,
)
@click.option(
    "--audience",
    "-a",
    help="The target audience for the model.",
    type=str,
    required=False,
)
@click.option(
    "--use-cases",
    "-u",
    help="The use cases of the model.",
    type=str,
    required=False,
)
@click.option(
    "--tradeoffs",
    help="The tradeoffs of the model.",
    type=str,
    required=False,
)
@click.option(
    "--ethical",
    "-e",
    help="The ethical implications of the model.",
    type=str,
    required=False,
)
@click.option(
    "--limitations",
    help="The known limitations of the model.",
    type=str,
    required=False,
)
@click.option(
    "--tag",
    "-t",
    help="Tags associated with the model.",
    type=str,
    required=False,
    multiple=True,
)
@click.option(
    "--save-models-to-registry",
    "-s",
    help="Whether to automatically save model artifacts to the model registry.",
    type=click.BOOL,
    required=False,
    default=True,
)
def register_model(
    name: str,
    license: Optional[str],
    description: Optional[str],
    audience: Optional[str],
    use_cases: Optional[str],
    tradeoffs: Optional[str],
    ethical: Optional[str],
    limitations: Optional[str],
    tag: Optional[List[str]],
    save_models_to_registry: Optional[bool],
) -> None:
    """Register a new model in the Model Control Plane.

    Args:
        name: The name of the model.
        license: The license model created under.
        description: The description of the model.
        audience: The target audience of the model.
        use_cases: The use cases of the model.
        tradeoffs: The tradeoffs of the model.
        ethical: The ethical implications of the model.
        limitations: The know limitations of the model.
        tag: Tags associated with the model.
        save_models_to_registry: Whether to save the model to the
            registry.
    """
    try:
        model = Client().create_model(
            **remove_none_values(
                dict(
                    name=name,
                    license=license,
                    description=description,
                    audience=audience,
                    use_cases=use_cases,
                    trade_offs=tradeoffs,
                    ethics=ethical,
                    limitations=limitations,
                    tags=tag,
                    save_models_to_registry=save_models_to_registry,
                )
            )
        )
    except (EntityExistsError, ValueError) as e:
        cli_utils.error(str(e))

    cli_utils.print_table([_model_to_print(model)])


@model.command("update", help="Update an existing model.")
@click.argument("model_name_or_id")
@click.option(
    "--name",
    "-n",
    help="The name of the model.",
    type=str,
    required=False,
)
@click.option(
    "--license",
    "-l",
    help="The license under which the model is created.",
    type=str,
    required=False,
)
@click.option(
    "--description",
    "-d",
    help="The description of the model.",
    type=str,
    required=False,
)
@click.option(
    "--audience",
    "-a",
    help="The target audience for the model.",
    type=str,
    required=False,
)
@click.option(
    "--use-cases",
    "-u",
    help="The use cases of the model.",
    type=str,
    required=False,
)
@click.option(
    "--tradeoffs",
    help="The tradeoffs of the model.",
    type=str,
    required=False,
)
@click.option(
    "--ethical",
    "-e",
    help="The ethical implications of the model.",
    type=str,
    required=False,
)
@click.option(
    "--limitations",
    help="The known limitations of the model.",
    type=str,
    required=False,
)
@click.option(
    "--tag",
    "-t",
    help="Tags to be added to the model.",
    type=str,
    required=False,
    multiple=True,
)
@click.option(
    "--remove-tag",
    "-r",
    help="Tags to be removed from the model.",
    type=str,
    required=False,
    multiple=True,
)
@click.option(
    "--save-models-to-registry",
    "-s",
    help="Whether to automatically save model artifacts to the model registry.",
    type=click.BOOL,
    required=False,
    default=True,
)
def update_model(
    model_name_or_id: str,
    name: Optional[str],
    license: Optional[str],
    description: Optional[str],
    audience: Optional[str],
    use_cases: Optional[str],
    tradeoffs: Optional[str],
    ethical: Optional[str],
    limitations: Optional[str],
    tag: Optional[List[str]],
    remove_tag: Optional[List[str]],
    save_models_to_registry: Optional[bool],
) -> None:
    """Register a new model in the Model Control Plane.

    Args:
        model_name_or_id: The name of the model.
        name: The name of the model.
        license: The license model created under.
        description: The description of the model.
        audience: The target audience of the model.
        use_cases: The use cases of the model.
        tradeoffs: The tradeoffs of the model.
        ethical: The ethical implications of the model.
        limitations: The know limitations of the model.
        tag: Tags to be added to the model.
        remove_tag: Tags to be removed from the model.
        save_models_to_registry: Whether to save the model to the
            registry.
    """
    model_id = Client().get_model(model_name_or_id=model_name_or_id).id
    update_dict = remove_none_values(
        dict(
            name=name,
            license=license,
            description=description,
            audience=audience,
            use_cases=use_cases,
            trade_offs=tradeoffs,
            ethics=ethical,
            limitations=limitations,
            add_tags=tag,
            remove_tags=remove_tag,
            save_models_to_registry=save_models_to_registry,
        )
    )
    model = Client().update_model(model_name_or_id=model_id, **update_dict)

    cli_utils.print_table([_model_to_print(model)])


@model.command("delete", help="Delete an existing model.")
@click.argument("model_name_or_id")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Don't ask for confirmation.",
)
def delete_model(
    model_name_or_id: str,
    yes: bool = False,
) -> None:
    """Delete an existing model from the Model Control Plane.

    Args:
        model_name_or_id: The ID or name of the model to delete.
        yes: If set, don't ask for confirmation.
    """
    if not yes:
        confirmation = cli_utils.confirmation(
            f"Are you sure you want to delete model '{model_name_or_id}'?"
        )
        if not confirmation:
            cli_utils.declare("Model deletion canceled.")
            return

    try:
        Client().delete_model(
            model_name_or_id=model_name_or_id,
        )
    except (KeyError, ValueError) as e:
        cli_utils.error(str(e))
    else:
        cli_utils.declare(f"Model '{model_name_or_id}' deleted.")


@model.group()
def version() -> None:
    """Interact with model versions in the Model Control Plane."""


@enhanced_list_options(ModelVersionFilter)
@version.command("list", help="List model versions with filter.")
def list_model_versions(**kwargs: Any) -> None:
    """List model versions with filter in the Model Control Plane.

    Args:
        **kwargs: Keyword arguments to filter models.
    """
    # Extract table options from kwargs
    table_kwargs = cli_utils.extract_table_options(kwargs)

    with console.status("Listing model versions..."):
        model_versions = Client().list_model_versions(**kwargs)

    if not model_versions:
        cli_utils.declare("No model versions found.")
        return

    # Prepare data based on output format
    output_format = (
        table_kwargs.get("output") or cli_utils.get_default_output_format()
    )
    model_version_data = []

    # Use centralized data preparation
    model_version_data = prepare_list_data(
        model_versions,
        output_format,
        _model_version_to_print,
        _model_version_to_print_full,
    )

    # Handle table output with enhanced system and pagination
    cli_utils.handle_table_output(
        model_version_data, page=model_versions, **table_kwargs
    )


@version.command("update", help="Update an existing model version stage.")
@click.argument("model_name_or_id")
@click.argument("model_version_name_or_number_or_id")
@click.option(
    "--stage",
    "-s",
    type=click.Choice(choices=ModelStages.values()),
    required=False,
    help="The stage of the model version.",
)
@click.option(
    "--name",
    "-n",
    type=str,
    required=False,
    help="The name of the model version.",
)
@click.option(
    "--description",
    "-d",
    type=str,
    required=False,
    help="The description of the model version.",
)
@click.option(
    "--tag",
    "-t",
    help="Tags to be added to the model.",
    type=str,
    required=False,
    multiple=True,
)
@click.option(
    "--remove-tag",
    "-r",
    help="Tags to be removed from the model.",
    type=str,
    required=False,
    multiple=True,
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Don't ask for confirmation, if stage already occupied.",
)
def update_model_version(
    model_name_or_id: str,
    model_version_name_or_number_or_id: str,
    stage: Optional[str],
    name: Optional[str],
    description: Optional[str],
    tag: Optional[List[str]],
    remove_tag: Optional[List[str]],
    force: bool = False,
) -> None:
    """Update an existing model version stage in the Model Control Plane.

    Args:
        model_name_or_id: The ID or name of the model containing version.
        model_version_name_or_number_or_id: The ID, number or name of the model version.
        stage: The stage of the model version to be set.
        name: The name of the model version.
        description: The description of the model version.
        tag: Tags to be added to the model version.
        remove_tag: Tags to be removed from the model version.
        force: Whether existing model version in target stage should be silently archived.
    """
    model_version = Client().get_model_version(
        model_name_or_id=model_name_or_id,
        model_version_name_or_number_or_id=model_version_name_or_number_or_id,
    )
    try:
        model_version = Client().update_model_version(
            model_name_or_id=model_name_or_id,
            version_name_or_id=model_version.id,
            stage=stage,
            add_tags=tag,
            remove_tags=remove_tag,
            force=force,
            name=name,
            description=description,
        )
    except RuntimeError:
        if not force:
            cli_utils.print_table([_model_version_to_print(model_version)])

            confirmation = cli_utils.confirmation(
                "Are you sure you want to change the status of model "
                f"version '{model_version_name_or_number_or_id}' to "
                f"'{stage}'?\nThis stage is already taken by "
                "model version shown above and if you will proceed this "
                "model version will get into archived stage."
            )
            if not confirmation:
                cli_utils.declare("Model version stage update canceled.")
                return
            model_version = Client().update_model_version(
                model_name_or_id=model_version.model.id,
                version_name_or_id=model_version.id,
                stage=stage,
                add_tags=tag,
                remove_tags=remove_tag,
                force=True,
                description=description,
            )
    cli_utils.print_table([_model_version_to_print(model_version)])


@version.command("delete", help="Delete an existing model version.")
@click.argument("model_name_or_id")
@click.argument("model_version_name_or_number_or_id")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Don't ask for confirmation.",
)
def delete_model_version(
    model_name_or_id: str,
    model_version_name_or_number_or_id: str,
    yes: bool = False,
) -> None:
    """Delete an existing model version in the Model Control Plane.

    Args:
        model_name_or_id: The ID or name of the model that contains the version.
        model_version_name_or_number_or_id: The ID, number or name of the model version.
        yes: If set, don't ask for confirmation.
    """
    if not yes:
        confirmation = cli_utils.confirmation(
            f"Are you sure you want to delete model version '{model_version_name_or_number_or_id}' from model '{model_name_or_id}'?"
        )
        if not confirmation:
            cli_utils.declare("Model version deletion canceled.")
            return

    try:
        model_version = Client().get_model_version(
            model_name_or_id=model_name_or_id,
            model_version_name_or_number_or_id=model_version_name_or_number_or_id,
        )
        Client().delete_model_version(
            model_version_id=model_version.id,
        )
    except (KeyError, ValueError) as e:
        cli_utils.error(str(e))
    else:
        cli_utils.declare(
            f"Model version '{model_version_name_or_number_or_id}' deleted from model '{model_name_or_id}'."
        )


def _artifact_link_to_print(link: Any) -> Dict[str, Any]:
    """Convert an artifact link response to a dictionary for table display.

    For table output, keep it compact with essential link information.
    Full details are available in JSON/YAML output formats.

    Args:
        link: Artifact link response object

    Returns:
        Dictionary containing formatted artifact link data for table display
    """
    return {
        "artifact_version": link.artifact_version.name
        if hasattr(link, "artifact_version") and link.artifact_version
        else "",
        "created": link.created.strftime("%Y-%m-%d %H:%M:%S")
        if hasattr(link, "created") and link.created
        else "",
    }


def _artifact_link_to_print_full(link: Any) -> Dict[str, Any]:
    """Convert artifact link response to complete dictionary for JSON/YAML.

    Args:
        link: Artifact link response object

    Returns:
        Complete dictionary containing all artifact link data
    """
    return link.model_dump(mode="json")


def _pipeline_run_link_to_print(link: Any) -> Dict[str, Any]:
    """Convert a pipeline run link response to a dictionary for table display.

    For table output, keep it compact with essential link information.
    Full details are available in JSON/YAML output formats.

    Args:
        link: Pipeline run link response object

    Returns:
        Dictionary containing formatted pipeline run link data for table display
    """
    return {
        "name": link.pipeline_run.name
        if hasattr(link, "pipeline_run") and link.pipeline_run
        else "",
        "pipeline_name": link.pipeline_run.pipeline.name
        if hasattr(link, "pipeline_run")
        and link.pipeline_run
        and hasattr(link.pipeline_run, "pipeline")
        and link.pipeline_run.pipeline
        else "",
        "status": link.pipeline_run.status.value
        if hasattr(link, "pipeline_run")
        and link.pipeline_run
        and hasattr(link.pipeline_run, "status")
        and hasattr(link.pipeline_run.status, "value")
        else str(link.pipeline_run.status)
        if hasattr(link, "pipeline_run")
        and link.pipeline_run
        and hasattr(link.pipeline_run, "status")
        else "",
        "created": link.created.strftime("%Y-%m-%d %H:%M:%S")
        if hasattr(link, "created") and link.created
        else "",
    }


def _pipeline_run_link_to_print_full(link: Any) -> Dict[str, Any]:
    """Convert pipeline run link response to complete dictionary for JSON/YAML.

    Args:
        link: Pipeline run link response object

    Returns:
        Complete dictionary containing all pipeline run link data
    """
    return link.model_dump(mode="json")


def _print_artifacts_links_generic(
    model_name_or_id: str,
    model_version_name_or_number_or_id: Optional[str] = None,
    only_data_artifacts: bool = False,
    only_deployment_artifacts: bool = False,
    only_model_artifacts: bool = False,
    **kwargs: Any,
) -> None:
    """Generic method to print artifacts links.

    Args:
        model_name_or_id: The ID or name of the model containing version.
        model_version_name_or_number_or_id: The name, number or ID of the model version.
        only_data_artifacts: If set, only print data artifacts.
        only_deployment_artifacts: If set, only print deployment artifacts.
        only_model_artifacts: If set, only print model artifacts.
        **kwargs: Keyword arguments to filter models.
    """
    # Extract table options from kwargs
    table_kwargs = cli_utils.extract_table_options(kwargs)

    model_version = Client().get_model_version(
        model_name_or_id=model_name_or_id,
        model_version_name_or_number_or_id=model_version_name_or_number_or_id,
    )
    type_ = (
        "data artifacts"
        if only_data_artifacts
        else "deployment artifacts"
        if only_deployment_artifacts
        else "model artifacts"
    )

    with console.status(f"Listing {type_}..."):
        links = Client().list_model_version_artifact_links(
            model_version_id=model_version.id,
            only_data_artifacts=only_data_artifacts,
            only_deployment_artifacts=only_deployment_artifacts,
            only_model_artifacts=only_model_artifacts,
            **kwargs,
        )

    if not links:
        cli_utils.declare(f"No {type_} linked to the model version found.")
        return

    # Prepare data based on output format
    output_format = (
        table_kwargs.get("output") or cli_utils.get_default_output_format()
    )

    # Handle both paginated and non-paginated responses
    link_list = links.items if hasattr(links, "items") else links

    # Use centralized data preparation
    link_data = prepare_list_data(
        link_list,
        output_format,
        _artifact_link_to_print,
        _artifact_link_to_print_full,
    )

    # Set title for table output
    if output_format == "table":
        table_kwargs["title"] = (
            f"{type_.title()} linked to model version `{model_version.name}[{model_version.number}]`"
        )

    # Handle table output with enhanced system and pagination
    cli_utils.handle_table_output(
        data=link_data,
        page=links if hasattr(links, "items") else None,
        **table_kwargs,
    )


@model.command(
    "data_artifacts",
    help="List data artifacts linked to a model version.",
)
@click.argument("model_name")
@click.option("--model_version", "-v", default=None)
@enhanced_list_options(ModelVersionArtifactFilter)
def list_model_version_data_artifacts(
    model_name: str,
    model_version: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """List data artifacts linked to a model version in the Model Control Plane.

    Args:
        model_name: The ID or name of the model containing version.
        model_version: The name, number or ID of the model version. If not
            provided, the latest version is used.
        **kwargs: Keyword arguments to filter models.
    """
    _print_artifacts_links_generic(
        model_name_or_id=model_name,
        model_version_name_or_number_or_id=model_version,
        only_data_artifacts=True,
        **kwargs,
    )


@model.command(
    "model_artifacts",
    help="List model artifacts linked to a model version.",
)
@click.argument("model_name")
@click.option("--model_version", "-v", default=None)
@enhanced_list_options(ModelVersionArtifactFilter)
def list_model_version_model_artifacts(
    model_name: str,
    model_version: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """List model artifacts linked to a model version in the Model Control Plane.

    Args:
        model_name: The ID or name of the model containing version.
        model_version: The name, number or ID of the model version. If not
            provided, the latest version is used.
        **kwargs: Keyword arguments to filter models.
    """
    _print_artifacts_links_generic(
        model_name_or_id=model_name,
        model_version_name_or_number_or_id=model_version,
        only_model_artifacts=True,
        **kwargs,
    )


@model.command(
    "deployment_artifacts",
    help="List deployment artifacts linked to a model version.",
)
@click.argument("model_name")
@click.option("--model_version", "-v", default=None)
@enhanced_list_options(ModelVersionArtifactFilter)
def list_model_version_deployment_artifacts(
    model_name: str,
    model_version: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """List deployment artifacts linked to a model version in the Model Control Plane.

    Args:
        model_name: The ID or name of the model containing version.
        model_version: The name, number or ID of the model version. If not
            provided, the latest version is used.
        **kwargs: Keyword arguments to filter models.
    """
    _print_artifacts_links_generic(
        model_name_or_id=model_name,
        model_version_name_or_number_or_id=model_version,
        only_deployment_artifacts=True,
        **kwargs,
    )


@model.command(
    "runs",
    help="List pipeline runs of a model version.",
)
@click.argument("model_name")
@click.option("--model_version", "-v", default=None)
@enhanced_list_options(ModelVersionPipelineRunFilter)
def list_model_version_pipeline_runs(
    model_name: str,
    model_version: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """List pipeline runs of a model version in the Model Control Plane.

    Args:
        model_name: The ID or name of the model containing version.
        model_version: The name, number or ID of the model version. If not
            provided, the latest version is used.
        **kwargs: Keyword arguments to filter runs.
    """
    model_version_response_model = Client().get_model_version(
        model_name_or_id=model_name,
        model_version_name_or_number_or_id=model_version,
    )

    runs = Client().list_model_version_pipeline_run_links(
        model_version_id=model_version_response_model.id,
        **kwargs,
    )

    if not runs:
        cli_utils.declare("No pipeline runs attached to model version found.")
        return

    cli_utils.title(
        f"Pipeline runs linked to the model version `{model_version_response_model.name}[{model_version_response_model.number}]`:"
    )
    cli_utils.print_pydantic_models(runs)
