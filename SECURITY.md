# Security Policy

## Supported Versions

Security corrections are provided for the current public release of
ShibbyPrints Kiosk Sorter Software. Users should upgrade to the most recent
public release before reporting a problem that may already have been corrected.

## Reporting a Vulnerability

Do not disclose suspected security vulnerabilities through a public GitHub
issue, discussion, or pull request.

Use the repository's **Security → Report a vulnerability** option to submit the
report privately to ShibbyPrints.

Alternatively, email the report privately to **shibbyprints@gmail.com**.

Include, when available:

- the affected Kiosk version;
- the internal version shown in an exported diagnostic report;
- the Windows version and system configuration;
- a description of the problem and its potential impact;
- clear steps to reproduce it; and
- a minimal proof of concept.

Do not include passwords, API keys, authentication tokens, private customer
data, or unnecessary production images. Diagnostic archives may contain camera
images, serial traffic, hardware details, system information, and local file
paths. Review them before sharing and provide them only when requested.

Please allow ShibbyPrints a reasonable opportunity to investigate and prepare
a correction before publicly disclosing the issue.

## Scope

This policy applies to ShibbyPrints Kiosk Sorter Software distributed and
maintained by ShibbyPrints.

The following are separate systems and are not maintained by ShibbyPrints as
part of this Kiosk release:

- sorter hardware and firmware;
- the local classification server;
- the hosted community service; and
- the original upstream desktop software.

Issues affecting those components should be reported to their respective
maintainers.

## Untrusted Content

Only import or download model files from sources you trust. PyTorch model
loading uses restricted weight-only loading, but model files and archives
should still be treated as untrusted input.

Only evaluate image folders from trusted sources. Generated evaluation reports
contain filenames, classifications, and embedded image data.

Diagnostic exports and evaluation reports should be treated as potentially
sensitive files.

When using a classification server outside the local computer, use an HTTPS
endpoint. Images and API credentials sent to an unencrypted remote HTTP
endpoint may be visible to others on the network.

## Community Features

Community sign-in, model sharing, model downloading, and feedback uploads are
optional. Normal operation does not require a community account.
