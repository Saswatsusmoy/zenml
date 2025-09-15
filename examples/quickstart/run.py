# Apache Software License 2.0
#
# Copyright (c) ZenML GmbH 2024. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
from typing import Optional

import click
from pipelines import (
    english_translation_inference,
    english_translation_training,
)

from zenml.client import Client
from zenml.logger import get_logger

logger = get_logger(__name__)


@click.command(
    help="""
ZenML Starter project.

Run the ZenML starter project with basic options.

Examples:

  \b
  # Run the training pipeline
    python run.py
"""
)
@click.option(
    "--no-cache",
    is_flag=True,
    default=False,
    help="Disable caching for the pipeline run.",
)
@click.option(
    "--model_type",
    type=click.Choice(["t5-small", "t5-large"], case_sensitive=False),
    default="t5-small",
    help="Choose the model size: t5-small or t5-large.",
)
@click.option(
    "--config_path",
    help="Choose the configuration file.",
)
@click.option(
    "--training",
    is_flag=True,
    default=False,
    help="Whether to run the training pipeline.",
)
@click.option(
    "--inference",
    is_flag=True,
    default=False,
    help="Whether to run the inference pipeline.",
)
def main(
    model_type: str,
    config_path: Optional[str],
    training: bool = False,
    inference: bool = False,
    no_cache: bool = False,
):
    """Main entry point for the pipeline execution.

    This entrypoint is where everything comes together:

      * configuring pipeline with the required parameters
        (some of which may come from command line arguments, but most
        of which comes from the YAML config files)
      * launching the pipeline

    Args:
        model_type: Type of model to use
        config_path: Configuration file to use
        training_pipeline: Whether to run the training pipeline.
        inference_pipeline: Whether to run the inference pipeline.
        no_cache: If `True` cache will be disabled.
    """
    if not training and not inference:
        print("No pipeline specified, running training pipeline by default.")
        training = True

    client = Client()

    orchf = client.active_stack.orchestrator.flavor

    sof = None
    if client.active_stack.step_operator:
        sof = client.active_stack.step_operator.flavor

    pipeline_args = {}
    if no_cache:
        pipeline_args["enable_cache"] = False

    if training:
        if not config_path:
            # Default configuration
            config_path = "configs/training_default.yaml"
            #
            if orchf == "sagemaker" or sof == "sagemaker":
                config_path = "configs/training_aws.yaml"
            elif orchf == "vertex" or sof == "vertex":
                config_path = "configs/training_gcp.yaml"
            elif orchf == "azureml" or sof == "azureml":
                config_path = "configs/training_azure.yaml"

            print(f"Using {config_path} to configure the pipeline run.")
        else:
            print(
                f"You specified {config_path}. Please be aware of the contents of this "
                f"file as some settings might be very specific to a certain orchestration "
                f"environment. Also you might need to set `skip_build` to False in case "
                f"of missing requirements in the execution environment."
            )

        pipeline_args["config_path"] = config_path
        english_translation_training.with_options(**pipeline_args)(
            model_type=model_type,
        )

    if inference:
        # Prompt for the data input
        data_input = input("Enter sentence to translate: ")
        # Default configuration
        config_path = "configs/inference_default.yaml"
        pipeline_args["config_path"] = config_path
        run = english_translation_inference.with_options(**pipeline_args)(
            input=data_input,
        )
        # Load and print the output of the last step of the last run
        run = client.get_pipeline_run(run.id)
        result = run.steps["call_model"].output.load()
        print(result)


if __name__ == "__main__":
    main()
