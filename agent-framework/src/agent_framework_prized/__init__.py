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

"""Prized Sandboxes for the Microsoft Agent Framework: an agent's shell commands in a microVM."""

from ._files import PrizedFileStore
from ._shell import PrizedShellTool

__version__ = "0.1.0"

__all__ = ["PrizedFileStore", "PrizedShellTool", "__version__"]
