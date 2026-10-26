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

"""Prized Sandboxes for Google ADK.

``PrizedEnvironment`` gives an agent a persistent remote workspace (shell,
files, installs) through ADK's ``EnvironmentToolset``; ``PrizedCodeExecutor``
runs the code blocks an agent writes. Both run in Prized sandboxes, Firecracker
microVMs that pause with their memory and fork into live copies.

Example:
  ```python
  from google.adk import Agent
  from google.adk.tools.environment import EnvironmentToolset
  from prized_adk import PrizedEnvironment

  root_agent = Agent(
      name="coder",
      model="gemini-flash-latest",
      instruction="Work in the sandbox.",
      tools=[EnvironmentToolset(environment=PrizedEnvironment())],
  )
  ```
"""

from ._code_executor import PrizedCodeExecutor
from ._environment import PrizedEnvironment

__version__ = "0.1.0"

__all__ = [
    "PrizedCodeExecutor",
    "PrizedEnvironment",
    "__version__",
]
