#  Copyright (c) ZenML GmbH 2025. All Rights Reserved.
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
"""Implementation of the AWS App Runner deployer."""

import json
import re
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Generator,
    List,
    Optional,
    Tuple,
    Type,
    cast,
)
from uuid import UUID

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel

from zenml.config.base_settings import BaseSettings
from zenml.config.resource_settings import ResourceSettings
from zenml.deployers.containerized_deployer import ContainerizedDeployer
from zenml.deployers.exceptions import (
    DeployerError,
    PipelineEndpointDeploymentError,
    PipelineEndpointDeprovisionError,
    PipelineEndpointNotFoundError,
    PipelineLogsNotFoundError,
)
from zenml.deployers.serving.entrypoint_configuration import (
    AUTH_KEY_OPTION,
    PORT_OPTION,
    ServingEntrypointConfiguration,
)
from zenml.entrypoints.base_entrypoint_configuration import (
    DEPLOYMENT_ID_OPTION,
)
from zenml.enums import PipelineEndpointStatus, StackComponentType
from zenml.integrations.aws.flavors.aws_deployer_flavor import (
    AWSDeployerConfig,
    AWSDeployerSettings,
)
from zenml.logger import get_logger
from zenml.models import (
    PipelineEndpointOperationalState,
    PipelineEndpointResponse,
)
from zenml.stack import StackValidator

if TYPE_CHECKING:
    from zenml.stack import Stack

logger = get_logger(__name__)

# Default resource and scaling configuration constants
# These are used when ResourceSettings are not provided in the pipeline configuration
DEFAULT_CPU = "0.25 vCPU"
DEFAULT_MEMORY = "0.5 GB"
DEFAULT_MIN_SIZE = 1
DEFAULT_MAX_SIZE = 25
DEFAULT_MAX_CONCURRENCY = 100

# AWS App Runner limits
AWS_APP_RUNNER_MAX_SIZE = 1000
AWS_APP_RUNNER_MAX_CONCURRENCY = 1000


class AppRunnerPipelineEndpointMetadata(BaseModel):
    """Metadata for an App Runner pipeline endpoint."""

    service_name: Optional[str] = None
    service_arn: Optional[str] = None
    service_url: Optional[str] = None
    region: Optional[str] = None
    service_id: Optional[str] = None
    status: Optional[str] = None
    source_configuration: Optional[Dict[str, Any]] = None
    instance_configuration: Optional[Dict[str, Any]] = None
    auto_scaling_configuration_summary: Optional[Dict[str, Any]] = None
    auto_scaling_configuration_arn: Optional[str] = None
    health_check_configuration: Optional[Dict[str, Any]] = None
    network_configuration: Optional[Dict[str, Any]] = None
    observability_configuration: Optional[Dict[str, Any]] = None
    encryption_configuration: Optional[Dict[str, Any]] = None
    cpu: Optional[str] = None
    memory: Optional[str] = None
    port: Optional[int] = None
    auto_scaling_max_concurrency: Optional[int] = None
    auto_scaling_max_size: Optional[int] = None
    auto_scaling_min_size: Optional[int] = None
    is_publicly_accessible: Optional[bool] = None
    health_check_grace_period_seconds: Optional[int] = None
    health_check_interval_seconds: Optional[int] = None
    health_check_path: Optional[str] = None
    health_check_protocol: Optional[str] = None
    health_check_timeout_seconds: Optional[int] = None
    health_check_healthy_threshold: Optional[int] = None
    health_check_unhealthy_threshold: Optional[int] = None
    tags: Optional[Dict[str, str]] = None
    environment_variables: Optional[Dict[str, str]] = None
    traffic_allocation: Optional[Dict[str, int]] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    deleted_at: Optional[str] = None
    secret_arn: Optional[str] = None

    @classmethod
    def from_app_runner_service(
        cls,
        service: Dict[str, Any],
        region: str,
        secret_arn: Optional[str] = None,
    ) -> "AppRunnerPipelineEndpointMetadata":
        """Create metadata from an App Runner service.

        Args:
            service: The App Runner service dictionary from describe_service.
            region: The AWS region.
            secret_arn: The AWS Secrets Manager secret ARN for the pipeline endpoint.

        Returns:
            The metadata for the App Runner service.
        """
        # Extract instance configuration
        instance_config = service.get("InstanceConfiguration", {})
        cpu = instance_config.get("Cpu")
        memory = instance_config.get("Memory")

        # Extract auto scaling configuration
        auto_scaling_config = service.get(
            "AutoScalingConfigurationSummary", {}
        )
        auto_scaling_configuration_arn = auto_scaling_config.get(
            "AutoScalingConfigurationArn"
        )
        auto_scaling_max_concurrency = auto_scaling_config.get(
            "MaxConcurrency"
        )
        auto_scaling_max_size = auto_scaling_config.get("MaxSize")
        auto_scaling_min_size = auto_scaling_config.get("MinSize")

        # Extract health check configuration
        health_check_config = service.get("HealthCheckConfiguration", {})
        health_check_grace_period = health_check_config.get(
            "HealthCheckGracePeriodSeconds"
        )
        health_check_interval = health_check_config.get("Interval")
        health_check_path = health_check_config.get("Path")
        health_check_protocol = health_check_config.get("Protocol")
        health_check_timeout = health_check_config.get("Timeout")
        health_check_healthy_threshold = health_check_config.get(
            "HealthyThreshold"
        )
        health_check_unhealthy_threshold = health_check_config.get(
            "UnhealthyThreshold"
        )

        # Extract network configuration
        network_config = service.get("NetworkConfiguration", {})
        is_publicly_accessible = network_config.get(
            "IngressConfiguration", {}
        ).get("IsPubliclyAccessible")

        # Extract source configuration and environment variables
        source_config = service.get("SourceConfiguration", {})
        image_repo = source_config.get("ImageRepository", {})
        image_config = image_repo.get("ImageConfiguration", {})

        port = None
        env_vars = {}
        if image_config:
            port = image_config.get("Port")
            runtime_env_vars = image_config.pop(
                "RuntimeEnvironmentVariables", {}
            )
            env_vars = dict(runtime_env_vars) if runtime_env_vars else {}
            # Note: We don't extract RuntimeEnvironmentSecrets for security reasons

        # Extract traffic allocation
        traffic_allocation = {}
        traffic_config = service.get("TrafficConfiguration", [])
        for traffic in traffic_config:
            if traffic.get("Type") == "LATEST":
                traffic_allocation["LATEST"] = traffic.get("Percent", 0)
            elif traffic.get("Revision"):
                traffic_allocation[traffic["Revision"]] = traffic.get(
                    "Percent", 0
                )
            elif traffic.get("Tag"):
                traffic_allocation[f"tag:{traffic['Tag']}"] = traffic.get(
                    "Percent", 0
                )

        # Extract timestamps
        created_at = service.get("CreatedAt")
        updated_at = service.get("UpdatedAt")
        deleted_at = service.get("DeletedAt")

        return cls(
            service_name=service.get("ServiceName"),
            service_arn=service.get("ServiceArn"),
            service_url=service.get("ServiceUrl"),
            region=region,
            service_id=service.get("ServiceId"),
            status=service.get("Status"),
            source_configuration=source_config,
            instance_configuration=instance_config,
            auto_scaling_configuration_summary=auto_scaling_config,
            auto_scaling_configuration_arn=auto_scaling_configuration_arn,
            health_check_configuration=health_check_config,
            network_configuration=network_config,
            observability_configuration=service.get(
                "ObservabilityConfiguration"
            ),
            encryption_configuration=service.get("EncryptionConfiguration"),
            cpu=cpu,
            memory=memory,
            port=port,
            auto_scaling_max_concurrency=auto_scaling_max_concurrency,
            auto_scaling_max_size=auto_scaling_max_size,
            auto_scaling_min_size=auto_scaling_min_size,
            is_publicly_accessible=is_publicly_accessible,
            health_check_grace_period_seconds=health_check_grace_period,
            health_check_interval_seconds=health_check_interval,
            health_check_path=health_check_path,
            health_check_protocol=health_check_protocol,
            health_check_timeout_seconds=health_check_timeout,
            health_check_healthy_threshold=health_check_healthy_threshold,
            health_check_unhealthy_threshold=health_check_unhealthy_threshold,
            tags=dict(service.get("Tags", {})),
            environment_variables=env_vars,
            traffic_allocation=traffic_allocation
            if traffic_allocation
            else None,
            created_at=created_at.isoformat() if created_at else None,
            updated_at=updated_at.isoformat() if updated_at else None,
            deleted_at=deleted_at.isoformat() if deleted_at else None,
            secret_arn=secret_arn,
        )

    @classmethod
    def from_endpoint(
        cls, endpoint: PipelineEndpointResponse
    ) -> "AppRunnerPipelineEndpointMetadata":
        """Create metadata from a pipeline endpoint.

        Args:
            endpoint: The pipeline endpoint to get the metadata for.

        Returns:
            The metadata for the pipeline endpoint.
        """
        return cls.model_validate(endpoint.endpoint_metadata)


class AWSDeployer(ContainerizedDeployer):
    """Deployer responsible for serving pipelines on AWS App Runner."""

    CONTAINER_REQUIREMENTS: List[str] = ["uvicorn", "fastapi"]

    _boto_session: Optional[boto3.Session] = None
    _region: Optional[str] = None
    _app_runner_client: Optional[Any] = None
    _secrets_manager_client: Optional[Any] = None
    _logs_client: Optional[Any] = None

    @property
    def config(self) -> AWSDeployerConfig:
        """Returns the `AWSDeployerConfig` config.

        Returns:
            The configuration.
        """
        return cast(AWSDeployerConfig, self._config)

    @property
    def settings_class(self) -> Optional[Type["BaseSettings"]]:
        """Settings class for the AWS deployer.

        Returns:
            The settings class.
        """
        return AWSDeployerSettings

    @property
    def validator(self) -> Optional[StackValidator]:
        """Ensures there is an image builder in the stack.

        Returns:
            A `StackValidator` instance.
        """
        return StackValidator(
            required_components={
                StackComponentType.IMAGE_BUILDER,
                StackComponentType.CONTAINER_REGISTRY,
            }
        )

    def _get_boto_session_and_region(self) -> Tuple[boto3.Session, str]:
        """Get an authenticated boto3 session and determine the region.

        Returns:
            A tuple containing the boto3 session and the AWS region.

        Raises:
            RuntimeError: If the service connector returns an unexpected type.
        """
        # Check if we need to refresh the session (e.g., connector expired)
        if (
            self._boto_session is not None
            and self._region is not None
            and not self.connector_has_expired()
        ):
            return self._boto_session, self._region

        # Option 1: Service connector
        if connector := self.get_connector():
            boto_session = connector.connect()
            if not isinstance(boto_session, boto3.Session):
                raise RuntimeError(
                    f"Expected to receive a `boto3.Session` object from the "
                    f"linked connector, but got type `{type(boto_session)}`."
                )

            # Get region from the session
            region = boto_session.region_name
            if not region:
                # Fallback to config region or default
                region = self.config.region or "us-east-1"
                logger.warning(
                    f"No region found in boto3 session, using {region}"
                )
        # Option 2: Implicit configuration
        else:
            boto_session = boto3.Session(region_name=self.config.region)

        self._boto_session = boto_session
        self._region = region
        return boto_session, region

    @property
    def app_runner_client(self) -> Any:
        """Get the App Runner client.

        Returns:
            The App Runner client.
        """
        if self._app_runner_client is None or self.connector_has_expired():
            session, region = self._get_boto_session_and_region()
            self._app_runner_client = session.client(
                "apprunner", region_name=region
            )
        return self._app_runner_client

    @property
    def secrets_manager_client(self) -> Any:
        """Get the Secrets Manager client.

        Returns:
            The Secrets Manager client.
        """
        if (
            self._secrets_manager_client is None
            or self.connector_has_expired()
        ):
            session, region = self._get_boto_session_and_region()
            self._secrets_manager_client = session.client(
                "secretsmanager", region_name=region
            )
        return self._secrets_manager_client

    @property
    def logs_client(self) -> Any:
        """Get the CloudWatch Logs client.

        Returns:
            The CloudWatch Logs client.
        """
        if self._logs_client is None or self.connector_has_expired():
            session, region = self._get_boto_session_and_region()
            self._logs_client = session.client("logs", region_name=region)
        return self._logs_client

    @property
    def region(self) -> str:
        """Get the AWS region.

        Returns:
            The AWS region.
        """
        _, region = self._get_boto_session_and_region()
        return region

    def _sanitize_app_runner_service_name(
        self, name: str, random_suffix: str
    ) -> str:
        """Sanitize a name to comply with App Runner service naming requirements.

        App Runner service name requirements:
        - Length: 4-40 characters
        - Characters: letters (a-z, A-Z), numbers (0-9), hyphens (-)
        - Must start and end with a letter or number
        - Cannot contain consecutive hyphens

        Args:
            name: The raw name to sanitize.
            random_suffix: A random suffix to add to the name to ensure
                uniqueness. Assumed to be valid.

        Returns:
            A sanitized name that complies with App Runner requirements.

        Raises:
            RuntimeError: If the random suffix is invalid.
            ValueError: If the service name is invalid.
        """
        # Validate the random suffix
        if not re.match(r"^[a-zA-Z0-9-]+$", random_suffix):
            raise RuntimeError(
                f"Invalid random suffix: {random_suffix}. Must contain only "
                "letters, numbers, and hyphens."
            )

        # Replace all disallowed characters with hyphens
        sanitized = re.sub(r"[^a-zA-Z0-9-]", "-", name)

        # Remove consecutive hyphens
        sanitized = re.sub(r"-+", "-", sanitized)

        # Ensure it starts and ends with alphanumeric
        sanitized = sanitized.strip("-")

        # Ensure it starts with a letter or number
        if not sanitized or not sanitized[0].isalnum():
            raise ValueError(
                f"Invalid service name: {name}. Must start with a letter or number."
            )

        # Ensure it ends with a letter or number
        if not sanitized[-1].isalnum():
            sanitized = sanitized.rstrip("-")

        # Ensure we have at least one character after cleanup
        if not sanitized:
            raise ValueError(
                f"Invalid service name: {name}. Must contain valid characters."
            )

        # Truncate to fit within 40 character limit including suffix
        max_base_length = 40 - len(random_suffix) - 1  # -1 for the hyphen
        if len(sanitized) > max_base_length:
            sanitized = sanitized[:max_base_length]
            # Make sure we don't end with a hyphen after truncation
            sanitized = sanitized.rstrip("-")

        # Final safety check
        if (
            not sanitized
            or not sanitized[0].isalnum()
            or not sanitized[-1].isalnum()
        ):
            raise ValueError(
                f"Invalid service name: {name}. Must start and end with alphanumeric characters."
            )

        final_name = f"{sanitized}-{random_suffix}"

        # Ensure final name meets length requirements (4-40 characters)
        if len(final_name) < 4 or len(final_name) > 40:
            raise ValueError(
                f"Service name '{final_name}' must be between 4-40 characters."
            )

        return final_name

    def _get_service_name(
        self, endpoint_name: str, endpoint_id: UUID, prefix: str
    ) -> str:
        """Get the App Runner service name for a pipeline endpoint.

        Args:
            endpoint_name: The pipeline endpoint name.
            endpoint_id: The pipeline endpoint ID.
            prefix: The prefix to use for the service name.

        Returns:
            The App Runner service name that complies with all naming requirements.
        """
        # Create a base name with endpoint name and ID for uniqueness
        # Use first 8 characters of UUID to keep names manageable
        endpoint_id_short = str(endpoint_id)[:8]
        raw_name = f"{prefix}{endpoint_name}"

        return self._sanitize_app_runner_service_name(
            raw_name, endpoint_id_short
        )

    def _sanitize_auto_scaling_config_name(self, name: str) -> str:
        """Sanitize a name to comply with App Runner AutoScalingConfiguration naming requirements.

        AutoScalingConfiguration name requirements:
        - Length: 4-32 characters
        - Characters: letters (a-z, A-Z), numbers (0-9), hyphens (-)
        - Must start with a letter or number
        - Cannot end with a hyphen
        - Must be unique per region and account

        Args:
            name: The raw name to sanitize.

        Returns:
            A sanitized name that complies with AutoScalingConfiguration requirements.

        Raises:
            ValueError: If the name cannot be sanitized to meet requirements.
        """
        # Remove invalid characters, keep letters, numbers, hyphens
        sanitized = re.sub(r"[^a-zA-Z0-9-]", "-", name)

        # Remove consecutive hyphens
        sanitized = re.sub(r"-+", "-", sanitized)

        # Ensure it starts with a letter or number
        if not sanitized or not (sanitized[0].isalnum()):
            raise ValueError(
                f"Invalid auto-scaling config name: {name}. Must start with a letter or number."
            )

        # Remove trailing hyphens
        sanitized = sanitized.rstrip("-")

        # Ensure we have at least one character after cleanup
        if not sanitized:
            raise ValueError(
                f"Invalid auto-scaling config name: {name}. Must start with a letter or number."
            )

        # Truncate to 32 characters (AutoScalingConfiguration limit)
        if len(sanitized) > 32:
            sanitized = sanitized[:32]
            # Make sure we don't end with a hyphen after truncation
            sanitized = sanitized.rstrip("-")

        # Final safety check - ensure minimum length of 4
        if len(sanitized) < 4:
            # Pad with endpoint ID prefix if too short
            sanitized = f"zenml-{sanitized}"[:32].rstrip("-")

        return sanitized

    def _sanitize_secret_name(self, name: str, random_suffix: str) -> str:
        """Sanitize a name to comply with Secrets Manager naming requirements.

        Secrets Manager secret name requirements:
        - Length: 1-512 characters
        - Characters: letters, numbers, hyphens, underscores, periods, forward slashes
        - Cannot start or end with forward slash
        - Cannot contain consecutive forward slashes

        Args:
            name: The raw name to sanitize.
            random_suffix: A random suffix to add to the name to ensure
                uniqueness.

        Returns:
            A sanitized name that complies with Secrets Manager requirements.

        Raises:
            ValueError: If the secret name is invalid.
        """
        # Validate the random suffix
        if not re.match(r"^[a-zA-Z0-9_-]+$", random_suffix):
            raise RuntimeError(
                f"Invalid random suffix: {random_suffix}. Must contain only "
                "letters, numbers, hyphens, and underscores."
            )

        # Replace disallowed characters with underscores
        sanitized = re.sub(r"[^a-zA-Z0-9_.-/]", "_", name)

        # Remove consecutive forward slashes
        sanitized = re.sub(r"/+", "/", sanitized)

        # Remove leading and trailing forward slashes
        sanitized = sanitized.strip("/")

        # Ensure we have at least one character after cleanup
        if not sanitized:
            raise ValueError(
                f"Invalid secret name: {name}. Must contain valid characters."
            )

        # Truncate to fit within 512 character limit including suffix
        max_base_length = 512 - len(random_suffix) - 1  # -1 for the underscore
        if len(sanitized) > max_base_length:
            sanitized = sanitized[:max_base_length]
            # Remove trailing forward slashes after truncation
            sanitized = sanitized.rstrip("/")

        # Final safety check
        if not sanitized:
            raise ValueError(
                f"Invalid secret name: {name}. Must contain valid characters."
            )

        return f"{sanitized}_{random_suffix}"

    def _get_secret_name(
        self,
        endpoint_name: str,
        endpoint_id: UUID,
        prefix: str,
    ) -> str:
        """Get the Secrets Manager secret name for a pipeline endpoint.

        Args:
            endpoint_name: The pipeline endpoint name.
            endpoint_id: The pipeline endpoint ID.
            prefix: The prefix to use for the secret name.

        Returns:
            The Secrets Manager secret name.
        """
        # Create a unique secret name with prefix and endpoint info
        endpoint_id_short = str(endpoint_id)[:8]
        raw_name = f"{prefix}{endpoint_name}"

        return self._sanitize_secret_name(raw_name, endpoint_id_short)

    def _create_or_update_secret(
        self,
        secret_name: str,
        secret_value: str,
        endpoint: PipelineEndpointResponse,
    ) -> str:
        """Create or update a secret in Secrets Manager.

        Args:
            secret_name: The name of the secret.
            secret_value: The value to store.
            endpoint: The pipeline endpoint.

        Returns:
            The secret ARN.

        Raises:
            DeployerError: If secret creation/update fails.
        """
        try:
            # Try to update existing secret
            try:
                response = self.secrets_manager_client.update_secret(
                    SecretId=secret_name,
                    SecretString=secret_value,
                )
                logger.debug(f"Updated existing secret {secret_name}")
                return response["ARN"]  # type: ignore[no-any-return]
            except ClientError as e:
                if e.response["Error"]["Code"] == "ResourceNotFoundException":
                    # Create new secret
                    logger.debug(f"Creating new secret {secret_name}")
                    response = self.secrets_manager_client.create_secret(
                        Name=secret_name,
                        SecretString=secret_value,
                        Description=f"ZenML pipeline endpoint secret for {endpoint.name}",
                        Tags=[
                            {
                                "Key": "zenml-pipeline-endpoint-uuid",
                                "Value": str(endpoint.id),
                            },
                            {
                                "Key": "zenml-pipeline-endpoint-name",
                                "Value": endpoint.name,
                            },
                            {
                                "Key": "zenml-deployer-name",
                                "Value": str(self.name),
                            },
                            {
                                "Key": "zenml-deployer-id",
                                "Value": str(self.id),
                            },
                            {"Key": "managed-by", "Value": "zenml"},
                        ],
                    )
                    logger.debug(f"Created new secret {secret_name}")
                    return response["ARN"]  # type: ignore[no-any-return]
                else:
                    raise

        except (ClientError, BotoCoreError) as e:
            raise DeployerError(
                f"Failed to create/update secret {secret_name}: {e}"
            )

    def _get_secret_arn(
        self, endpoint: PipelineEndpointResponse
    ) -> Optional[str]:
        """Get the existing AWS Secrets Manager secret ARN for a pipeline endpoint.

        Args:
            endpoint: The pipeline endpoint.

        Returns:
            The existing AWS Secrets Manager secret ARN for the pipeline endpoint,
            or None if no secret exists.
        """
        metadata = AppRunnerPipelineEndpointMetadata.from_endpoint(endpoint)

        if not metadata.secret_arn:
            return None

        try:
            # Verify the secret still exists
            self.secrets_manager_client.describe_secret(
                SecretId=metadata.secret_arn
            )
            return metadata.secret_arn
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                return None
            logger.exception(f"Failed to verify secret {metadata.secret_arn}")
            return None

    def _delete_secret(self, secret_arn: str) -> None:
        """Delete a secret from Secrets Manager.

        Args:
            secret_arn: The ARN of the secret to delete.
        """
        try:
            self.secrets_manager_client.delete_secret(
                SecretId=secret_arn,
                ForceDeleteWithoutRecovery=True,
            )
            logger.debug(f"Deleted secret {secret_arn}")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                logger.debug(
                    f"Secret {secret_arn} not found, skipping deletion"
                )
            else:
                logger.exception(f"Failed to delete secret {secret_arn}")

    def _cleanup_endpoint_secrets(
        self,
        endpoint: PipelineEndpointResponse,
    ) -> None:
        """Clean up the secret associated with a pipeline endpoint.

        Args:
            endpoint: The pipeline endpoint.
        """
        secret_arn = self._get_secret_arn(endpoint)

        if secret_arn:
            self._delete_secret(secret_arn)

    def _get_auto_scaling_config_name(
        self, endpoint_name: str, endpoint_id: UUID
    ) -> str:
        """Get the AutoScalingConfiguration name for a pipeline endpoint.

        Args:
            endpoint_name: The pipeline endpoint name.
            endpoint_id: The pipeline endpoint ID.

        Returns:
            The AutoScalingConfiguration name.
        """
        # Use first 8 characters of UUID to keep names manageable
        endpoint_id_short = str(endpoint_id)[:8]
        raw_name = f"zenml-{endpoint_name}-{endpoint_id_short}"

        return self._sanitize_auto_scaling_config_name(raw_name)

    def _create_or_update_auto_scaling_config(
        self,
        config_name: str,
        min_size: int,
        max_size: int,
        max_concurrency: int,
        endpoint: PipelineEndpointResponse,
    ) -> str:
        """Create or update an AutoScalingConfiguration for App Runner.

        Args:
            config_name: The name for the auto-scaling configuration.
            min_size: Minimum number of instances.
            max_size: Maximum number of instances.
            max_concurrency: Maximum concurrent requests per instance.
            endpoint: The pipeline endpoint.

        Returns:
            The ARN of the created/updated auto-scaling configuration.

        Raises:
            DeployerError: If auto-scaling configuration creation/update fails.
        """
        try:
            # Prepare tags for the auto-scaling configuration
            tags = [
                {
                    "Key": "zenml-pipeline-endpoint-uuid",
                    "Value": str(endpoint.id),
                },
                {
                    "Key": "zenml-pipeline-endpoint-name",
                    "Value": endpoint.name,
                },
                {"Key": "zenml-deployer-name", "Value": str(self.name)},
                {"Key": "zenml-deployer-id", "Value": str(self.id)},
                {"Key": "managed-by", "Value": "zenml"},
            ]

            # Check if we have an existing auto-scaling configuration ARN from metadata
            existing_arn = self._get_auto_scaling_config_arn(endpoint)

            if existing_arn:
                # Try to get existing configuration by ARN
                try:
                    response = self.app_runner_client.describe_auto_scaling_configuration(
                        AutoScalingConfigurationArn=existing_arn
                    )
                    existing_config = response["AutoScalingConfiguration"]

                    # Check if update is needed
                    if (
                        existing_config["MaxConcurrency"] == max_concurrency
                        and existing_config["MaxSize"] == max_size
                        and existing_config["MinSize"] == min_size
                    ):
                        logger.debug(
                            f"Auto-scaling configuration {existing_arn} is up to date"
                        )
                        return existing_arn

                except ClientError as e:
                    if (
                        e.response["Error"]["Code"]
                        != "InvalidRequestException"
                    ):
                        raise
                    # ARN is invalid or configuration was deleted, we'll create a new one
                    logger.debug(
                        f"Existing auto-scaling configuration {existing_arn} not found, creating new one"
                    )

            # Create new auto-scaling configuration
            logger.debug(f"Creating auto-scaling configuration {config_name}")
            response = (
                self.app_runner_client.create_auto_scaling_configuration(
                    AutoScalingConfigurationName=config_name,
                    MaxConcurrency=max_concurrency,
                    MaxSize=max_size,
                    MinSize=min_size,
                    Tags=tags,
                )
            )

            return response["AutoScalingConfiguration"][  # type: ignore[no-any-return]
                "AutoScalingConfigurationArn"
            ]

        except (ClientError, BotoCoreError) as e:
            raise DeployerError(
                f"Failed to create/update auto-scaling configuration {config_name}: {e}"
            )

    def _get_auto_scaling_config_arn(
        self, endpoint: PipelineEndpointResponse
    ) -> Optional[str]:
        """Get the existing auto-scaling configuration ARN for a pipeline endpoint.

        Args:
            endpoint: The pipeline endpoint.

        Returns:
            The auto-scaling configuration ARN if it exists, None otherwise.
        """
        try:
            metadata = AppRunnerPipelineEndpointMetadata.from_endpoint(
                endpoint
            )
            return metadata.auto_scaling_configuration_arn
        except Exception:
            return None

    def _cleanup_endpoint_auto_scaling_config(
        self, endpoint: PipelineEndpointResponse
    ) -> None:
        """Clean up the auto-scaling configuration associated with a pipeline endpoint.

        Args:
            endpoint: The pipeline endpoint.
        """
        config_arn = self._get_auto_scaling_config_arn(endpoint)

        if config_arn:
            try:
                logger.debug(
                    f"Deleting auto-scaling configuration {config_arn}"
                )
                self.app_runner_client.delete_auto_scaling_configuration(
                    AutoScalingConfigurationArn=config_arn
                )
            except ClientError as e:
                if e.response["Error"]["Code"] == "ResourceNotFoundException":
                    logger.debug(
                        f"Auto-scaling configuration {config_arn} not found, skipping deletion"
                    )
                else:
                    logger.warning(
                        f"Failed to delete auto-scaling configuration {config_arn}: {e}"
                    )
            except Exception as e:
                logger.warning(
                    f"Failed to delete auto-scaling configuration {config_arn}: {e}"
                )

    def _prepare_environment_variables(
        self,
        endpoint: PipelineEndpointResponse,
        environment: Dict[str, str],
        secrets: Dict[str, str],
        settings: AWSDeployerSettings,
    ) -> Tuple[Dict[str, str], Dict[str, str], Optional[str]]:
        """Prepare environment variables for App Runner, handling secrets appropriately.

        Args:
            endpoint: The pipeline endpoint.
            environment: Regular environment variables.
            secrets: Sensitive environment variables.
            settings: The deployer settings.

        Returns:
            Tuple containing:
            - Dictionary of regular environment variables.
            - Dictionary of secret environment variables (key -> secret ARN).
            - Optional secret ARN (None if no secrets or fallback to env vars).
        """
        env_vars = {}
        secret_refs = {}
        active_secret_arn: Optional[str] = None

        # Handle regular environment variables
        merged_env = {**settings.environment_variables, **environment}
        env_vars.update(merged_env)

        # Handle secrets
        if secrets:
            if settings.use_secrets_manager:
                # Always store secrets as single JSON secret and reference keys
                # This approach works for both single and multiple secrets

                secret_name = self._get_secret_name(
                    endpoint.name, endpoint.id, settings.secret_name_prefix
                )

                try:
                    # Create or update the secret with JSON value
                    secret_value = json.dumps(secrets)
                    secret_arn = self._create_or_update_secret(
                        secret_name, secret_value, endpoint
                    )
                    active_secret_arn = secret_arn

                    # Reference individual keys from the combined secret
                    for key in secrets.keys():
                        # App Runner format: secret-arn:key::
                        secret_refs[key] = f"{secret_arn}:{key}::"

                    logger.debug(
                        f"Secret {secret_name} stored with ARN {secret_arn} "
                        f"containing {len(secrets)} secret(s)"
                    )

                except Exception as e:
                    logger.warning(
                        f"Failed to create secret, falling back "
                        f"to direct env vars: {e}"
                    )
                    # Fallback to direct environment variables
                    env_vars.update(secrets)

                # Clean up old secret if it's different from the current one
                existing_secret_arn = self._get_secret_arn(endpoint)
                if (
                    existing_secret_arn
                    and existing_secret_arn != active_secret_arn
                ):
                    self._delete_secret(existing_secret_arn)
            else:
                # Store secrets directly as environment variables (less secure)
                logger.warning(
                    "Storing secrets directly in environment variables. "
                    "Consider enabling use_secrets_manager for better security."
                )
                env_vars.update(secrets)

        return env_vars, secret_refs, active_secret_arn

    def _get_app_runner_service(
        self, endpoint: PipelineEndpointResponse
    ) -> Optional[Dict[str, Any]]:
        """Get an existing App Runner service for a pipeline endpoint.

        Args:
            endpoint: The pipeline endpoint.

        Returns:
            The App Runner service dictionary, or None if it doesn't exist.
        """
        # Get service ARN from the endpoint metadata
        existing_metadata = AppRunnerPipelineEndpointMetadata.from_endpoint(
            endpoint
        )

        if not existing_metadata.service_arn:
            return None

        try:
            response = self.app_runner_client.describe_service(
                ServiceArn=existing_metadata.service_arn
            )
            return response["Service"]  # type: ignore[no-any-return]
        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                return None
            raise

    def _get_service_operational_state(
        self,
        service: Dict[str, Any],
        region: str,
        secret_arn: Optional[str] = None,
    ) -> PipelineEndpointOperationalState:
        """Get the operational state of an App Runner service.

        Args:
            service: The App Runner service dictionary.
            region: The AWS region.
            secret_arn: The active Secrets Manager secret ARN.

        Returns:
            The operational state of the App Runner service.
        """
        metadata = AppRunnerPipelineEndpointMetadata.from_app_runner_service(
            service, region, secret_arn
        )

        state = PipelineEndpointOperationalState(
            status=PipelineEndpointStatus.UNKNOWN,
            metadata=metadata.model_dump(exclude_none=True),
        )

        # Map App Runner service status to ZenML status. Valid values are:
        # - CREATE_FAILED
        # - DELETE_FAILED
        # - RUNNING
        # - DELETED
        # - PAUSED
        # - OPERATION_IN_PROGRESS
        service_status = service.get("Status", "").upper()

        if service_status in [
            "CREATE_FAILED",
            "DELETE_FAILED",
        ]:
            state.status = PipelineEndpointStatus.ERROR
        elif service_status == "OPERATION_IN_PROGRESS":
            state.status = PipelineEndpointStatus.PENDING
        elif service_status == "RUNNING":
            state.status = PipelineEndpointStatus.RUNNING
            state.url = service.get("ServiceUrl")
            if state.url and not state.url.startswith("https://"):
                state.url = f"https://{state.url}"
        elif service_status == "DELETED":
            state.status = PipelineEndpointStatus.ABSENT
        elif service_status == "PAUSED":
            state.status = (
                PipelineEndpointStatus.PENDING
            )  # Treat paused as pending for now
        else:
            state.status = PipelineEndpointStatus.UNKNOWN

        return state

    def _requires_service_replacement(
        self,
        existing_service: Dict[str, Any],
        settings: AWSDeployerSettings,
    ) -> bool:
        """Check if the service configuration requires replacement.

        App Runner only requires service replacement for fundamental service-level
        changes that cannot be handled through revisions. Most configuration changes
        (image, resources, environment, scaling) can be handled as updates.

        Args:
            existing_service: The existing App Runner service.
            settings: The new deployer settings.

        Returns:
            True if the service needs to be replaced, False if it can be updated.
        """
        # Check if network access configuration changed (requires replacement)
        network_config = existing_service.get("NetworkConfiguration", {})
        ingress_config = network_config.get("IngressConfiguration", {})
        current_public_access = ingress_config.get("IsPubliclyAccessible")
        if current_public_access != settings.is_publicly_accessible:
            return True

        # Check if VPC configuration changed (requires replacement)
        current_vpc_config = network_config.get("EgressConfiguration", {})
        has_current_vpc = bool(current_vpc_config.get("VpcConnectorArn"))
        will_have_vpc = bool(settings.ingress_vpc_configuration)
        if has_current_vpc != will_have_vpc:
            return True

        # Check if encryption configuration changed (requires replacement)
        current_encryption = existing_service.get(
            "EncryptionConfiguration", {}
        )
        current_kms_key = current_encryption.get("KmsKey")
        if current_kms_key != settings.encryption_kms_key:
            return True

        # Everything else (image, CPU, memory, scaling, env vars, etc.)
        # can be handled as service updates with new revisions
        return False

    def _convert_resource_settings_to_aws_format(
        self,
        resource_settings: ResourceSettings,
    ) -> Tuple[str, str]:
        """Convert ResourceSettings to AWS App Runner resource format.

        AWS App Runner only supports specific CPU-memory combinations.
        This method selects the best combination that meets the requirements.

        Args:
            resource_settings: The resource settings from pipeline configuration.

        Returns:
            Tuple of (cpu, memory) in AWS App Runner format.
        """
        # Get requested resources
        requested_cpu = resource_settings.cpu_count
        requested_memory_gb = None
        if resource_settings.memory is not None:
            requested_memory_gb = resource_settings.get_memory(unit="GB")

        # Select the best CPU-memory combination
        cpu, memory = self._select_aws_cpu_memory_combination(
            requested_cpu, requested_memory_gb
        )

        return cpu, memory

    def _select_aws_cpu_memory_combination(
        self,
        requested_cpu: Optional[float],
        requested_memory_gb: Optional[float],
    ) -> Tuple[str, str]:
        """Select the best AWS App Runner CPU-memory combination.

        AWS App Runner only supports these specific combinations:
        - 0.25 vCPU: 0.5 GB, 1 GB
        - 0.5 vCPU: 1 GB
        - 1 vCPU: 2 GB, 3 GB, 4 GB
        - 2 vCPU: 4 GB, 6 GB
        - 4 vCPU: 8 GB, 10 GB, 12 GB

        Args:
            requested_cpu: Requested CPU count (can be None)
            requested_memory_gb: Requested memory in GB (can be None)

        Returns:
            Tuple of (cpu, memory) that best matches requirements
        """
        # Define valid AWS App Runner combinations (CPU -> [valid memory options])
        valid_combinations = [
            # (cpu_value, cpu_string, memory_value, memory_string)
            (0.25, "0.25 vCPU", 0.5, "0.5 GB"),
            (0.25, "0.25 vCPU", 1.0, "1 GB"),
            (0.5, "0.5 vCPU", 1.0, "1 GB"),
            (1.0, "1 vCPU", 2.0, "2 GB"),
            (1.0, "1 vCPU", 3.0, "3 GB"),
            (1.0, "1 vCPU", 4.0, "4 GB"),
            (2.0, "2 vCPU", 4.0, "4 GB"),
            (2.0, "2 vCPU", 6.0, "6 GB"),
            (4.0, "4 vCPU", 8.0, "8 GB"),
            (4.0, "4 vCPU", 10.0, "10 GB"),
            (4.0, "4 vCPU", 12.0, "12 GB"),
        ]

        # If no specific requirements, use default
        if requested_cpu is None and requested_memory_gb is None:
            return DEFAULT_CPU, DEFAULT_MEMORY

        # Find the best combination that satisfies both CPU and memory requirements
        best_combination = None
        best_score = float("inf")  # Lower is better

        for cpu_val, cpu_str, mem_val, mem_str in valid_combinations:
            # Check if this combination meets the requirements
            cpu_ok = requested_cpu is None or cpu_val >= requested_cpu
            mem_ok = (
                requested_memory_gb is None or mem_val >= requested_memory_gb
            )

            if cpu_ok and mem_ok:
                # Calculate "waste" score (how much over-provisioning)
                cpu_waste = (
                    0 if requested_cpu is None else (cpu_val - requested_cpu)
                )
                mem_waste = (
                    0
                    if requested_memory_gb is None
                    else (mem_val - requested_memory_gb)
                )

                # Prioritize CPU requirements, then memory
                score = cpu_waste * 10 + mem_waste

                if score < best_score:
                    best_score = score
                    best_combination = (cpu_str, mem_str)

        # If no combination satisfies requirements, use the highest available
        if best_combination is None:
            # Use the maximum available combination
            return "4 vCPU", "12 GB"

        return best_combination

    def _convert_scaling_settings_to_aws_format(
        self,
        resource_settings: ResourceSettings,
    ) -> Tuple[int, int, int]:
        """Convert ResourceSettings scaling to AWS App Runner format.

        Args:
            resource_settings: The resource settings from pipeline configuration.

        Returns:
            Tuple of (min_size, max_size, max_concurrency) for AWS App Runner.
        """
        min_size = DEFAULT_MIN_SIZE
        if resource_settings.min_replicas is not None:
            min_size = max(
                1, resource_settings.min_replicas
            )  # AWS App Runner min is 1

        max_size = DEFAULT_MAX_SIZE
        if resource_settings.max_replicas is not None:
            # ResourceSettings uses 0 to mean "no limit"
            # AWS App Runner needs a specific value, so we use the platform maximum
            if resource_settings.max_replicas == 0:
                max_size = AWS_APP_RUNNER_MAX_SIZE
            else:
                max_size = min(
                    resource_settings.max_replicas, AWS_APP_RUNNER_MAX_SIZE
                )

        max_concurrency = DEFAULT_MAX_CONCURRENCY
        if resource_settings.max_concurrency is not None:
            max_concurrency = min(
                resource_settings.max_concurrency,
                AWS_APP_RUNNER_MAX_CONCURRENCY,
            )

        return min_size, max_size, max_concurrency

    def do_provision_pipeline_endpoint(
        self,
        endpoint: PipelineEndpointResponse,
        stack: "Stack",
        environment: Dict[str, str],
        secrets: Dict[str, str],
        timeout: int,
    ) -> PipelineEndpointOperationalState:
        """Serve a pipeline as an App Runner service.

        Args:
            endpoint: The pipeline endpoint to serve.
            stack: The stack the pipeline will be served on.
            environment: Environment variables to set.
            secrets: Secret environment variables to set.
            timeout: The maximum time in seconds to wait for the pipeline
                endpoint to be deployed.

        Returns:
            The operational state of the deployed pipeline endpoint.

        Raises:
            PipelineEndpointDeploymentError: If the deployment fails.
            DeployerError: If an unexpected error occurs.
        """
        deployment = endpoint.pipeline_deployment
        assert deployment, "Pipeline deployment not found"

        environment = environment or {}
        secrets = secrets or {}

        settings = cast(
            AWSDeployerSettings,
            self.get_settings(deployment),
        )

        resource_settings = deployment.pipeline_configuration.resource_settings

        # Convert ResourceSettings to AWS App Runner format with fallbacks
        cpu, memory = self._convert_resource_settings_to_aws_format(
            resource_settings,
        )
        min_size, max_size, max_concurrency = (
            self._convert_scaling_settings_to_aws_format(
                resource_settings,
            )
        )

        client = self.app_runner_client

        service_name = self._get_service_name(
            endpoint.name, endpoint.id, settings.service_name_prefix
        )

        # Check if service already exists and if replacement is needed
        existing_service = self._get_app_runner_service(endpoint)
        image = self.get_image(deployment)
        region = self.region

        if existing_service and self._requires_service_replacement(
            existing_service, settings
        ):
            # Delete existing service before creating new one
            try:
                self.do_deprovision_pipeline_endpoint(endpoint, timeout)
            except PipelineEndpointNotFoundError:
                logger.warning(
                    f"Pipeline endpoint '{endpoint.name}' not found, "
                    f"skipping deprovision of existing App Runner service"
                )
            except DeployerError as e:
                logger.warning(
                    f"Failed to deprovision existing App Runner service for "
                    f"pipeline endpoint '{endpoint.name}': {e}"
                )
            existing_service = None

        # Prepare entrypoint and arguments
        entrypoint = ServingEntrypointConfiguration.get_entrypoint_command()
        arguments = ServingEntrypointConfiguration.get_entrypoint_arguments(
            **{
                DEPLOYMENT_ID_OPTION: deployment.id,
                PORT_OPTION: settings.port,
                AUTH_KEY_OPTION: endpoint.auth_key,
            }
        )

        # Prepare environment variables with proper secret handling
        env_vars, secret_refs, active_secret_arn = (
            self._prepare_environment_variables(
                endpoint, environment, secrets, settings
            )
        )

        # Determine the image repository type based on the image URI
        if "public.ecr.aws" in image:
            image_repo_type = "ECR_PUBLIC"
        elif "amazonaws.com" in image:
            image_repo_type = "ECR"
        else:
            # For other registries, we might need to handle differently
            image_repo_type = "ECR_PUBLIC"  # Default fallback

        # Build the image configuration
        image_config: Dict[str, Any] = {
            "Port": str(settings.port),
            "StartCommand": " ".join(entrypoint + arguments),
        }

        # Add regular environment variables if any
        if env_vars:
            image_config["RuntimeEnvironmentVariables"] = env_vars

        # Add secret references if any
        if secret_refs:
            image_config["RuntimeEnvironmentSecrets"] = secret_refs

        # Build the source configuration
        image_repository_config = {
            "ImageIdentifier": image,
            "ImageConfiguration": image_config,
            "ImageRepositoryType": image_repo_type,
        }

        source_configuration = {
            "ImageRepository": image_repository_config,
            # We don't want to automatically deploy new revisions when new
            # container images are pushed to the repository.
            "AutoDeploymentsEnabled": False,
        }

        # Add authentication configuration if access role is specified (required for private ECR)
        if settings.access_role_arn:
            source_configuration["AuthenticationConfiguration"] = {
                "AccessRoleArn": settings.access_role_arn
            }
        elif image_repo_type == "ECR":
            # Private ECR without explicit access role - warn user
            logger.warning(
                "Using private ECR repository without explicit access_role_arn. "
                "Ensure the default App Runner service role has ECR access permissions, "
                "or specify access_role_arn in deployer settings."
            )

        instance_configuration = {
            "Cpu": cpu,
            "Memory": memory,
        }
        # Only add InstanceRoleArn if it's actually provided
        if settings.instance_role_arn:
            instance_configuration["InstanceRoleArn"] = (
                settings.instance_role_arn
            )
        elif secret_refs:
            # If we're using secrets but no explicit role is provided,
            # App Runner will use the default service role which needs
            # secretsmanager:GetSecretValue permissions for the secret
            logger.warning(
                "Using secrets without explicit instance role. Ensure the default "
                "App Runner service role has secretsmanager:GetSecretValue permissions."
            )

        # Create or get auto-scaling configuration
        auto_scaling_config_name = self._get_auto_scaling_config_name(
            endpoint.name, endpoint.id
        )
        auto_scaling_config_arn = self._create_or_update_auto_scaling_config(
            auto_scaling_config_name,
            min_size,
            max_size,
            max_concurrency,
            endpoint,
        )

        health_check_configuration = {
            "Protocol": settings.health_check_protocol,
            "Interval": settings.health_check_interval_seconds,
            "Timeout": settings.health_check_timeout_seconds,
            "HealthyThreshold": settings.health_check_healthy_threshold,
            "UnhealthyThreshold": settings.health_check_unhealthy_threshold,
        }

        # Only add Path for HTTP health checks
        if settings.health_check_protocol.upper() == "HTTP":
            health_check_configuration["Path"] = settings.health_check_path

        network_configuration = {
            "IngressConfiguration": {
                "IsPubliclyAccessible": settings.is_publicly_accessible,
            }
        }

        # Prepare traffic allocation for App Runner
        traffic_configurations = []
        for revision, percent in settings.traffic_allocation.items():
            if revision == "LATEST":
                traffic_configurations.append(
                    {
                        "Type": "LATEST",
                        "Percent": percent,
                    }
                )
            else:
                # Check if it's a tag or revision name
                if revision.startswith("tag:"):
                    traffic_configurations.append(
                        {
                            "Tag": revision[4:],  # Remove "tag:" prefix
                            "Percent": percent,
                        }
                    )
                else:
                    traffic_configurations.append(
                        {
                            "Revision": revision,
                            "Percent": percent,
                        }
                    )

        # Add VPC configuration if specified
        if settings.ingress_vpc_configuration:
            vpc_config = json.loads(settings.ingress_vpc_configuration)
            network_configuration["IngressConfiguration"][
                "VpcIngressConnectionConfiguration"
            ] = vpc_config

        # Add encryption configuration if specified
        encryption_configuration = None
        if settings.encryption_kms_key:
            encryption_configuration = {
                "KmsKey": settings.encryption_kms_key,
            }

        # Add observability configuration if specified
        observability_configuration = None
        if settings.observability_configuration_arn:
            observability_configuration = {
                "ObservabilityEnabled": True,
                "ObservabilityConfigurationArn": settings.observability_configuration_arn,
            }

        # Prepare tags
        service_tags = [
            {"Key": "zenml-pipeline-endpoint-uuid", "Value": str(endpoint.id)},
            {"Key": "zenml-pipeline-endpoint-name", "Value": endpoint.name},
            {"Key": "zenml-deployer-name", "Value": str(self.name)},
            {"Key": "zenml-deployer-id", "Value": str(self.id)},
            {"Key": "managed-by", "Value": "zenml"},
        ]

        # Add user-defined tags
        for key, value in settings.tags.items():
            service_tags.append({"Key": key, "Value": value})

        try:
            if existing_service:
                # Update existing service
                logger.debug(
                    f"Updating existing App Runner service for pipeline "
                    f"endpoint '{endpoint.name}'"
                )

                update_request = {
                    "ServiceArn": existing_service["ServiceArn"],
                    "SourceConfiguration": source_configuration,
                    "InstanceConfiguration": instance_configuration,
                    "AutoScalingConfigurationArn": auto_scaling_config_arn,
                    "HealthCheckConfiguration": health_check_configuration,
                    "NetworkConfiguration": network_configuration,
                }

                # Add traffic configuration for updates (reuse the same logic)
                if not (
                    len(traffic_configurations) == 1
                    and traffic_configurations[0].get("Type") == "LATEST"
                    and traffic_configurations[0].get("Percent") == 100
                ):
                    update_request["TrafficConfiguration"] = (
                        traffic_configurations
                    )

                if encryption_configuration:
                    update_request["EncryptionConfiguration"] = (
                        encryption_configuration
                    )

                if observability_configuration:
                    update_request["ObservabilityConfiguration"] = (
                        observability_configuration
                    )

                response = client.update_service(**update_request)
                service_arn = response["Service"]["ServiceArn"]

                # Update tags separately
                client.tag_resource(
                    ResourceArn=service_arn,
                    Tags=service_tags,
                )

                updated_service = response["Service"]
            else:
                # Create new service
                logger.debug(
                    f"Creating new App Runner service for pipeline endpoint "
                    f"'{endpoint.name}' in region {region}"
                )

                create_request = {
                    "ServiceName": service_name,
                    "SourceConfiguration": source_configuration,
                    "InstanceConfiguration": instance_configuration,
                    "AutoScalingConfigurationArn": auto_scaling_config_arn,
                    "Tags": service_tags,
                    "HealthCheckConfiguration": health_check_configuration,
                    "NetworkConfiguration": network_configuration,
                }

                if encryption_configuration:
                    create_request["EncryptionConfiguration"] = (
                        encryption_configuration
                    )

                if observability_configuration:
                    create_request["ObservabilityConfiguration"] = (
                        observability_configuration
                    )

                # Only add traffic configuration if it's not the default (100% LATEST)
                if not (
                    len(traffic_configurations) == 1
                    and traffic_configurations[0].get("Type") == "LATEST"
                    and traffic_configurations[0].get("Percent") == 100
                ):
                    create_request["TrafficConfiguration"] = (
                        traffic_configurations
                    )

                response = client.create_service(**create_request)
                updated_service = response["Service"]

            return self._get_service_operational_state(
                updated_service, region, active_secret_arn
            )

        except (ClientError, BotoCoreError) as e:
            raise PipelineEndpointDeploymentError(
                f"Failed to deploy App Runner service for pipeline endpoint "
                f"'{endpoint.name}': {e}"
            )
        except Exception as e:
            raise DeployerError(
                f"Unexpected error while deploying pipeline endpoint "
                f"'{endpoint.name}': {e}"
            )

    def do_get_pipeline_endpoint(
        self,
        endpoint: PipelineEndpointResponse,
    ) -> PipelineEndpointOperationalState:
        """Get information about an App Runner pipeline endpoint.

        Args:
            endpoint: The pipeline endpoint to get information about.

        Returns:
            The operational state of the pipeline endpoint.

        Raises:
            PipelineEndpointNotFoundError: If the endpoint is not found.
            RuntimeError: If the service ARN is not found in the endpoint metadata.
        """
        service = self._get_app_runner_service(endpoint)

        if service is None:
            raise PipelineEndpointNotFoundError(
                f"App Runner service for pipeline endpoint '{endpoint.name}' "
                "not found"
            )

        existing_metadata = AppRunnerPipelineEndpointMetadata.from_endpoint(
            endpoint
        )

        if not existing_metadata.region:
            raise RuntimeError(
                f"Region not found in endpoint metadata for "
                f"pipeline endpoint '{endpoint.name}'"
            )

        existing_secret_arn = self._get_secret_arn(endpoint)

        return self._get_service_operational_state(
            service,
            existing_metadata.region,
            existing_secret_arn,
        )

    def do_get_pipeline_endpoint_logs(
        self,
        endpoint: PipelineEndpointResponse,
        follow: bool = False,
        tail: Optional[int] = None,
    ) -> Generator[str, bool, None]:
        """Get the logs of an App Runner pipeline endpoint.

        Args:
            endpoint: The pipeline endpoint to get the logs of.
            follow: If True, stream logs as they are written.
            tail: Only retrieve the last NUM lines of log output.

        Returns:
            A generator that yields the logs of the pipeline endpoint.

        Raises:
            PipelineEndpointNotFoundError: If the endpoint is not found.
            PipelineLogsNotFoundError: If the logs are not found.
            DeployerError: If an unexpected error occurs.
            RuntimeError: If the service name is not found in the endpoint metadata.
        """
        # If follow is requested, we would need to implement streaming
        if follow:
            raise NotImplementedError(
                "Log following is not yet implemented for App Runner deployer"
            )

        service = self._get_app_runner_service(endpoint)
        if service is None:
            raise PipelineEndpointNotFoundError(
                f"App Runner service for pipeline endpoint '{endpoint.name}' not found"
            )

        try:
            existing_metadata = (
                AppRunnerPipelineEndpointMetadata.from_endpoint(endpoint)
            )
            service_name = existing_metadata.service_name
            if not service_name:
                raise RuntimeError(
                    f"Service name not found in endpoint metadata for "
                    f"pipeline endpoint '{endpoint.name}'"
                )

            # App Runner automatically creates CloudWatch log groups
            log_group_name = f"/aws/apprunner/{service_name}/service"

            # Get log streams
            try:
                streams_response = self.logs_client.describe_log_streams(
                    logGroupName=log_group_name,
                    orderBy="LastEventTime",
                    descending=True,
                )

                log_lines = []
                for stream in streams_response.get("logStreams", []):
                    stream_name = stream["logStreamName"]

                    # Get events from this stream
                    events_response = self.logs_client.get_log_events(
                        logGroupName=log_group_name,
                        logStreamName=stream_name,
                        startFromHead=False,  # Get most recent first
                    )

                    for event in events_response.get("events", []):
                        timestamp = event.get("timestamp", 0)
                        message = event.get("message", "")

                        # Convert timestamp to readable format
                        import datetime

                        dt = datetime.datetime.fromtimestamp(
                            timestamp / 1000.0
                        )
                        formatted_time = dt.isoformat()

                        log_line = f"[{formatted_time}] {message}"
                        log_lines.append(log_line)

                # Sort by timestamp (most recent last for tail to work correctly)
                log_lines.sort()

                # Apply tail limit if specified
                if tail is not None and tail > 0:
                    log_lines = log_lines[-tail:]

                # Yield logs
                for log_line in log_lines:
                    yield log_line

            except ClientError as e:
                if e.response["Error"]["Code"] == "ResourceNotFoundException":
                    raise PipelineLogsNotFoundError(
                        f"Log group not found for App Runner service '{service_name}'"
                    )
                raise

        except (ClientError, BotoCoreError) as e:
            raise PipelineLogsNotFoundError(
                f"Failed to retrieve logs for pipeline endpoint '{endpoint.name}': {e}"
            )
        except Exception as e:
            raise DeployerError(
                f"Unexpected error while retrieving logs for pipeline endpoint '{endpoint.name}': {e}"
            )

    def do_deprovision_pipeline_endpoint(
        self,
        endpoint: PipelineEndpointResponse,
        timeout: int,
    ) -> Optional[PipelineEndpointOperationalState]:
        """Deprovision an App Runner pipeline endpoint.

        Args:
            endpoint: The pipeline endpoint to deprovision.
            timeout: The maximum time in seconds to wait for the pipeline
                endpoint to be deprovisioned.

        Returns:
            The operational state of the deprovisioned endpoint, or None if
            deletion is completed immediately.

        Raises:
            PipelineEndpointNotFoundError: If the endpoint is not found.
            PipelineEndpointDeprovisionError: If the deprovision fails.
            DeployerError: If an unexpected error occurs.
            RuntimeError: If the service ARN is not found in the endpoint metadata.
        """
        service = self._get_app_runner_service(endpoint)
        if service is None:
            raise PipelineEndpointNotFoundError(
                f"App Runner service for pipeline endpoint '{endpoint.name}' not found"
            )

        try:
            existing_metadata = (
                AppRunnerPipelineEndpointMetadata.from_endpoint(endpoint)
            )
            if not existing_metadata.service_arn:
                raise RuntimeError(
                    f"Service ARN not found in endpoint metadata for "
                    f"pipeline endpoint '{endpoint.name}'"
                )

            logger.debug(
                f"Deleting App Runner service for pipeline endpoint '{endpoint.name}'"
            )

            # Delete the service
            self.app_runner_client.delete_service(
                ServiceArn=existing_metadata.service_arn
            )

        except ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                raise PipelineEndpointNotFoundError(
                    f"App Runner service for pipeline endpoint '{endpoint.name}' not found"
                )
            raise PipelineEndpointDeprovisionError(
                f"Failed to delete App Runner service for pipeline endpoint '{endpoint.name}': {e}"
            )
        except Exception as e:
            raise DeployerError(
                f"Unexpected error while deleting pipeline endpoint '{endpoint.name}': {e}"
            )

        endpoint_before_deletion = endpoint

        # App Runner deletion is asynchronous and the auto-scaling configuration
        # and secrets need to be cleaned up after the service is deleted. So we
        # poll the service until it is deleted, runs into an error or times out.
        endpoint, endpoint_state = self._poll_pipeline_endpoint(
            endpoint, PipelineEndpointStatus.ABSENT, timeout
        )

        if endpoint_state.status != PipelineEndpointStatus.ABSENT:
            return endpoint_state

        try:
            # Clean up associated secrets
            self._cleanup_endpoint_secrets(endpoint_before_deletion)

            # Clean up associated auto-scaling configuration
            self._cleanup_endpoint_auto_scaling_config(
                endpoint_before_deletion
            )
        except Exception as e:
            raise DeployerError(
                f"Unexpected error while cleaning up resources for pipeline "
                f"endpoint '{endpoint.name}': {e}"
            )

        return None
