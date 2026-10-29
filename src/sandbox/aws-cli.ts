const AWS_CLI_VERSION = "2.34.54";
const AWS_CLI_SHA256 = "de278754dec97e0f6e9b4e8167d4bd1a27004c3e56e9c2da10002597f76ca35a";

const awsCliInstall = (installArgs = ""): string =>
  `curl -fsSL -o /tmp/awscliv2.zip https://awscli.amazonaws.com/awscli-exe-linux-x86_64-${AWS_CLI_VERSION}.zip` +
  ` && echo "${AWS_CLI_SHA256}  /tmp/awscliv2.zip" | sha256sum -c -` +
  ` && unzip -q /tmp/awscliv2.zip -d /tmp && /tmp/aws/install${installArgs} && rm -rf /tmp/aws /tmp/awscliv2.zip && aws --version`;

export const AWS_CLI_INSTALL = awsCliInstall();

export const AWS_CLI_ENSURE = `command -v aws >/dev/null 2>&1 || { ${AWS_CLI_INSTALL}; }`;

export const AWS_CLI_HOME_ENSURE = `command -v aws >/dev/null 2>&1 || { ${awsCliInstall(' -i "$HOME/.local/aws-cli" -b "$HOME/.local/bin"')}; }`;
