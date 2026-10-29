const GH_CLI_VERSION = "2.93.0";
const GH_CLI_SHA256 = "02d1290eba130e0b896f3709ffff22e1c75a51475ddb70476a85abc6b5807af0";
const GH_CLI_DIR = `/tmp/gh_${GH_CLI_VERSION}_linux_amd64`;

export const GH_CLI_HOME_ENSURE =
  `command -v gh >/dev/null 2>&1 || { curl -fsSL -o /tmp/gh.tgz https://github.com/cli/cli/releases/download/v${GH_CLI_VERSION}/gh_${GH_CLI_VERSION}_linux_amd64.tar.gz` +
  ` && echo "${GH_CLI_SHA256}  /tmp/gh.tgz" | sha256sum -c - && tar -xzf /tmp/gh.tgz -C /tmp` +
  ` && install -D -m 755 ${GH_CLI_DIR}/bin/gh "$HOME/.local/bin/gh" && rm -rf /tmp/gh.tgz ${GH_CLI_DIR} && gh --version; }`;
