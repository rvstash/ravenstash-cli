# Security policy

## Supported versions

Security fixes are made for the latest release in each channel marked `supported` in Ravenstash's signed channel manifest. Earlier minor targets remain available for controlled upgrades but do not represent separately supported security lines. Upgrade to the latest supported channel release before reporting a version-specific problem when practical.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use [GitHub private vulnerability reporting](https://github.com/rvstash/ravenstash-cli/security/advisories/new) and include:

- the affected rvs version and operating system;
- a minimal reproduction or proof of concept;
- the impact you observed or expect; and
- any suggested mitigation, if known.

Do not include real Ravenstash credentials, tokens, private package contents, or customer data. Use synthetic values and redact logs before attaching them.

Maintainers will acknowledge the report, investigate it privately, and coordinate disclosure and a fixed release. Please allow a reasonable remediation window before publishing details.

## Scope

This policy covers this CLI, its installers, and release artifacts. For account, billing, hosted-service, or abuse concerns, follow [SUPPORT.md](SUPPORT.md).
