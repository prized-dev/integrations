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


def test_unpatched_calls_cannot_reach_prized():
    """The conftest guard: a real create fails fast instead of making a sandbox."""
    with pytest.raises(prized.PrizedError) as info:
        prized.Sandbox.create(timeout=5, max_retries=0)
    assert info.value.code in ("network_error", "timeout", "unauthorized")
