# Copyright 2026 Prized
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The conftest guard holds: unit tests cannot create real sandboxes."""

import prized
import pytest

from stirrup_prized import PrizedCodeExecToolProvider
from tests import conftest


async def test_an_unfaked_provider_cannot_reach_prized():
    before = len(conftest.BLOCKED)
    with pytest.raises(prized.PrizedError) as info:
        await PrizedCodeExecToolProvider(sandbox_kwargs={"max_retries": 0}, request_timeout=5).__aenter__()
    assert info.value.code in ("network_error", "timeout", "unauthorized")
    assert conftest.BLOCKED[before:], "the request was not stopped by the guard"
    assert all(url.startswith("http://127.0.0.1:9/") for url in conftest.BLOCKED[before:])
