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

"""Prized Sandboxes for CAMEL: run an agent's code, shell commands and tools in a Firecracker microVM.

from camel_prized import PrizedCodeExecutionToolkit

with PrizedCodeExecutionToolkit() as toolkit:
    agent = ChatAgent("You write and run code.", tools=toolkit.get_tools())
"""

from .interpreter import PrizedInterpreter
from .runtime import PrizedRuntime
from .toolkit import PrizedCodeExecutionToolkit

__version__ = "0.1.0"

__all__ = ["PrizedCodeExecutionToolkit", "PrizedInterpreter", "PrizedRuntime", "__version__"]
