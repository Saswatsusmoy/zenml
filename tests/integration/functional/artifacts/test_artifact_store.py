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

import os

from zenml.client import Client
from zenml.utils.string_utils import random_str


def test_artifact_store_remove_on_folders(clean_client: Client) -> None:
    """Tests the artifact store's `rmtree` functionality for removing folders.

    This test verifies that the `rmtree` method of an artifact store correctly
    removes an entire folder, including its contents (e.g., a test file),
    from the artifact store's storage. It checks both the artifact store's
    perspective (`artifact_store.exists`) and the underlying file system's
    perspective (`os.path.exists`).

    Args:
        clean_client: A ZenML client instance with a clean environment,
            providing access to the active stack's artifact store.
    """
    artifact_store = clean_client.active_stack.artifact_store
    folder = os.path.join(artifact_store.path, "test_folder_" + random_str(10))
    artifact_store.makedirs(folder)

    with artifact_store.open(os.path.join(folder, "test.txt"), "w") as f:
        f.write("test")

    artifact_store.rmtree(folder)
    assert not artifact_store.exists(folder)
    assert not os.path.exists(folder)
