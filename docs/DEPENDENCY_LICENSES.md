# Locked dependency license inventory

Reviewed 2026-09-04 against `requirements.lock`, `requirements-dev.lock`, and
`package-lock.json`. Python declarations come from version-specific PyPI metadata
and the installed distribution license files where metadata is incomplete. npm
declarations come from the lockfile. Links identify the exact package release.

This is a package-level source-publication inventory, not a complete license
clearance or binary SBOM. Bundled native libraries can have additional licenses;
see [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md). Update this inventory
when the lockfiles change. Runtime packages also appear in the development lock.

## Python runtime

| Package | Version | Declared license / qualification |
| --- | --- | --- |
| [alembic](https://pypi.org/project/alembic/1.19.1/) | 1.19.1 | MIT |
| [annotated-doc](https://pypi.org/project/annotated-doc/0.0.5/) | 0.0.5 | MIT |
| [annotated-types](https://pypi.org/project/annotated-types/0.8.0/) | 0.8.0 | MIT |
| [anyio](https://pypi.org/project/anyio/4.14.2/) | 4.14.2 | MIT |
| [argon2-cffi](https://pypi.org/project/argon2-cffi/25.1.0/) | 25.1.0 | MIT |
| [argon2-cffi-bindings](https://pypi.org/project/argon2-cffi-bindings/26.1.0/) | 26.1.0 | MIT |
| [certifi](https://pypi.org/project/certifi/2026.7.22/) | 2026.7.22 | MPL-2.0 |
| [cffi](https://pypi.org/project/cffi/2.1.1/) | 2.1.1 | MIT-0 |
| [click](https://pypi.org/project/click/8.5.0/) | 8.5.0 | BSD-3-Clause |
| [dramatiq](https://pypi.org/project/dramatiq/2.2.0/) | 2.2.0 | LGPL-3.0-or-later |
| [fastapi](https://pypi.org/project/fastapi/0.141.1/) | 0.141.1 | MIT |
| [greenlet](https://pypi.org/project/greenlet/3.5.5/) | 3.5.5 | MIT AND PSF-2.0 |
| [h11](https://pypi.org/project/h11/0.16.0/) | 0.16.0 | MIT |
| [httpcore](https://pypi.org/project/httpcore/1.0.9/) | 1.0.9 | BSD-3-Clause |
| [httptools](https://pypi.org/project/httptools/0.8.0/) | 0.8.0 | MIT |
| [httpx](https://pypi.org/project/httpx/0.28.1/) | 0.28.1 | BSD-3-Clause |
| [idna](https://pypi.org/project/idna/3.19/) | 3.19 | BSD-3-Clause |
| [jinja2](https://pypi.org/project/jinja2/3.1.6/) | 3.1.6 | BSD-3-Clause |
| [mako](https://pypi.org/project/mako/1.4.1/) | 1.4.1 | MIT |
| [markdown-it-py](https://pypi.org/project/markdown-it-py/4.2.0/) | 4.2.0 | MIT |
| [markupsafe](https://pypi.org/project/markupsafe/3.0.3/) | 3.0.3 | BSD-3-Clause |
| [mdurl](https://pypi.org/project/mdurl/0.1.2/) | 0.1.2 | MIT |
| [orjson](https://pypi.org/project/orjson/3.12.0/) | 3.12.0 | MPL-2.0 AND (Apache-2.0 OR MIT) |
| [pillow](https://pypi.org/project/pillow/12.3.0/) | 12.3.0 | MIT-CMU |
| [pillow-heif](https://pypi.org/project/pillow-heif/1.5.0/) | 1.5.0 | BSD-3-Clause wrapper; GPL/LGPL wheel components |
| [playwright](https://pypi.org/project/playwright/1.62.0/) | 1.62.0 | Apache-2.0 |
| [psycopg](https://pypi.org/project/psycopg/3.3.4/) | 3.3.4 | LGPL-3.0-only |
| [psycopg-binary](https://pypi.org/project/psycopg-binary/3.3.4/) | 3.3.4 | LGPL-3.0-only |
| [pycparser](https://pypi.org/project/pycparser/3.0/) | 3.0 | BSD-3-Clause |
| [pydantic](https://pypi.org/project/pydantic/2.13.5/) | 2.13.5 | MIT |
| [pydantic-core](https://pypi.org/project/pydantic-core/2.46.5/) | 2.46.5 | MIT |
| [pydantic-settings](https://pypi.org/project/pydantic-settings/2.15.0/) | 2.15.0 | MIT |
| [pyee](https://pypi.org/project/pyee/13.0.1/) | 13.0.1 | MIT |
| [pygments](https://pypi.org/project/pygments/2.21.0/) | 2.21.0 | BSD-2-Clause |
| [pypdfium2](https://pypi.org/project/pypdfium2/5.13.0/) | 5.13.0 | Apache-2.0 OR BSD-3-Clause; additional binary licenses |
| [python-dotenv](https://pypi.org/project/python-dotenv/1.2.3/) | 1.2.3 | BSD-3-Clause |
| [python-multipart](https://pypi.org/project/python-multipart/0.0.32/) | 0.0.32 | Apache-2.0 |
| [pyyaml](https://pypi.org/project/pyyaml/6.0.3/) | 6.0.3 | MIT |
| [redis](https://pypi.org/project/redis/6.4.0/) | 6.4.0 | MIT |
| [rich](https://pypi.org/project/rich/15.0.0/) | 15.0.0 | MIT |
| [shellingham](https://pypi.org/project/shellingham/1.5.4/) | 1.5.4 | ISC |
| [sqlalchemy](https://pypi.org/project/sqlalchemy/2.0.52/) | 2.0.52 | MIT |
| [starlette](https://pypi.org/project/starlette/1.6.0/) | 1.6.0 | BSD-3-Clause |
| [typer](https://pypi.org/project/typer/0.27.2/) | 0.27.2 | MIT |
| [typing-extensions](https://pypi.org/project/typing-extensions/4.16.0/) | 4.16.0 | PSF-2.0 |
| [typing-inspection](https://pypi.org/project/typing-inspection/0.4.4/) | 0.4.4 | MIT |
| [tzdata](https://pypi.org/project/tzdata/2026.3/) | 2026.3 | Apache-2.0 |
| [uvicorn](https://pypi.org/project/uvicorn/0.52.4/) | 0.52.4 | BSD-3-Clause |
| [uvloop](https://pypi.org/project/uvloop/0.22.1/) | 0.22.1 | MIT OR Apache-2.0; bundled libuv notices |
| [watchfiles](https://pypi.org/project/watchfiles/1.2.0/) | 1.2.0 | MIT |
| [websockets](https://pypi.org/project/websockets/17.1/) | 17.1 | BSD-3-Clause |

## Additional Python development dependencies

| Package | Version | Declared license / qualification |
| --- | --- | --- |
| [boolean-py](https://pypi.org/project/boolean-py/5.0/) | 5.0 | BSD-2-Clause |
| [cachecontrol](https://pypi.org/project/cachecontrol/0.14.4/) | 0.14.4 | Apache-2.0 |
| [charset-normalizer](https://pypi.org/project/charset-normalizer/3.5.1/) | 3.5.1 | MIT |
| [coverage](https://pypi.org/project/coverage/7.16.0/) | 7.16.0 | Apache-2.0 |
| [cyclonedx-python-lib](https://pypi.org/project/cyclonedx-python-lib/11.12.0/) | 11.12.0 | Apache-2.0 |
| [defusedxml](https://pypi.org/project/defusedxml/0.7.1/) | 0.7.1 | PSF-2.0 |
| [filelock](https://pypi.org/project/filelock/3.32.4/) | 3.32.4 | MIT |
| [iniconfig](https://pypi.org/project/iniconfig/2.3.0/) | 2.3.0 | MIT |
| [librt](https://pypi.org/project/librt/0.15.0/) | 0.15.0 | MIT |
| [license-expression](https://pypi.org/project/license-expression/30.4.4/) | 30.4.4 | Apache-2.0 |
| [msgpack](https://pypi.org/project/msgpack/1.2.2/) | 1.2.2 | Apache-2.0 |
| [mypy](https://pypi.org/project/mypy/1.20.2/) | 1.20.2 | MIT |
| [mypy-extensions](https://pypi.org/project/mypy-extensions/1.1.0/) | 1.1.0 | MIT |
| [packageurl-python](https://pypi.org/project/packageurl-python/0.17.6/) | 0.17.6 | MIT |
| [packaging](https://pypi.org/project/packaging/26.3/) | 26.3 | Apache-2.0 OR BSD-2-Clause |
| [pathspec](https://pypi.org/project/pathspec/1.1.1/) | 1.1.1 | MPL-2.0 |
| [pip-api](https://pypi.org/project/pip-api/0.0.34/) | 0.0.34 | Apache-2.0 |
| [pip-audit](https://pypi.org/project/pip-audit/2.10.1/) | 2.10.1 | Apache-2.0 |
| [pip-requirements-parser](https://pypi.org/project/pip-requirements-parser/32.0.1/) | 32.0.1 | MIT |
| [platformdirs](https://pypi.org/project/platformdirs/4.11.5/) | 4.11.5 | MIT |
| [pluggy](https://pypi.org/project/pluggy/1.6.0/) | 1.6.0 | MIT |
| [py-serializable](https://pypi.org/project/py-serializable/2.1.0/) | 2.1.0 | Apache-2.0 |
| [pyparsing](https://pypi.org/project/pyparsing/3.3.2/) | 3.3.2 | MIT |
| [pytest](https://pypi.org/project/pytest/9.1.1/) | 9.1.1 | MIT |
| [pytest-base-url](https://pypi.org/project/pytest-base-url/2.1.0/) | 2.1.0 | MPL-2.0 |
| [pytest-cov](https://pypi.org/project/pytest-cov/7.1.0/) | 7.1.0 | MIT |
| [pytest-playwright](https://pypi.org/project/pytest-playwright/0.9.0/) | 0.9.0 | Apache-2.0 |
| [python-slugify](https://pypi.org/project/python-slugify/8.0.4/) | 8.0.4 | MIT |
| [requests](https://pypi.org/project/requests/2.34.2/) | 2.34.2 | Apache-2.0 |
| [ruff](https://pypi.org/project/ruff/0.16.5/) | 0.16.5 | MIT |
| [sortedcontainers](https://pypi.org/project/sortedcontainers/2.4.0/) | 2.4.0 | Apache-2.0 |
| [text-unidecode](https://pypi.org/project/text-unidecode/1.3/) | 1.3 | Artistic / GPL alternatives; see package license |
| [tomli](https://pypi.org/project/tomli/2.4.1/) | 2.4.1 | MIT |
| [tomli-w](https://pypi.org/project/tomli-w/1.2.0/) | 1.2.0 | MIT |
| [urllib3](https://pypi.org/project/urllib3/2.7.0/) | 2.7.0 | MIT |
| [pip](https://pypi.org/project/pip/26.2.1/) | 26.2.1 | MIT |

## Frontend build dependencies

Includes optional platform-specific packages; only matching binaries are installed.

| Package | Version | Declared license |
| --- | --- | --- |
| [@oxc-project/types](https://www.npmjs.com/package/@oxc-project/types/v/0.147.0) | 0.147.0 | MIT |
| [@rolldown/binding-android-arm-eabi](https://www.npmjs.com/package/@rolldown/binding-android-arm-eabi/v/1.2.6) | 1.2.6 | MIT |
| [@rolldown/binding-android-arm64](https://www.npmjs.com/package/@rolldown/binding-android-arm64/v/1.2.6) | 1.2.6 | MIT |
| [@rolldown/binding-darwin-arm64](https://www.npmjs.com/package/@rolldown/binding-darwin-arm64/v/1.2.6) | 1.2.6 | MIT |
| [@rolldown/binding-darwin-x64](https://www.npmjs.com/package/@rolldown/binding-darwin-x64/v/1.2.6) | 1.2.6 | MIT |
| [@rolldown/binding-freebsd-x64](https://www.npmjs.com/package/@rolldown/binding-freebsd-x64/v/1.2.6) | 1.2.6 | MIT |
| [@rolldown/binding-linux-arm-gnueabihf](https://www.npmjs.com/package/@rolldown/binding-linux-arm-gnueabihf/v/1.2.6) | 1.2.6 | MIT |
| [@rolldown/binding-linux-arm64-gnu](https://www.npmjs.com/package/@rolldown/binding-linux-arm64-gnu/v/1.2.6) | 1.2.6 | MIT |
| [@rolldown/binding-linux-arm64-musl](https://www.npmjs.com/package/@rolldown/binding-linux-arm64-musl/v/1.2.6) | 1.2.6 | MIT |
| [@rolldown/binding-linux-ppc64-gnu](https://www.npmjs.com/package/@rolldown/binding-linux-ppc64-gnu/v/1.2.6) | 1.2.6 | MIT |
| [@rolldown/binding-linux-s390x-gnu](https://www.npmjs.com/package/@rolldown/binding-linux-s390x-gnu/v/1.2.6) | 1.2.6 | MIT |
| [@rolldown/binding-linux-x64-gnu](https://www.npmjs.com/package/@rolldown/binding-linux-x64-gnu/v/1.2.6) | 1.2.6 | MIT |
| [@rolldown/binding-linux-x64-musl](https://www.npmjs.com/package/@rolldown/binding-linux-x64-musl/v/1.2.6) | 1.2.6 | MIT |
| [@rolldown/binding-openharmony-arm64](https://www.npmjs.com/package/@rolldown/binding-openharmony-arm64/v/1.2.6) | 1.2.6 | MIT |
| [@rolldown/binding-win32-arm64-msvc](https://www.npmjs.com/package/@rolldown/binding-win32-arm64-msvc/v/1.2.6) | 1.2.6 | MIT |
| [@rolldown/binding-win32-x64-msvc](https://www.npmjs.com/package/@rolldown/binding-win32-x64-msvc/v/1.2.6) | 1.2.6 | MIT |
| [@rolldown/pluginutils](https://www.npmjs.com/package/@rolldown/pluginutils/v/1.0.1) | 1.0.1 | MIT |
| [detect-libc](https://www.npmjs.com/package/detect-libc/v/2.1.2) | 2.1.2 | Apache-2.0 |
| [fdir](https://www.npmjs.com/package/fdir/v/6.5.0) | 6.5.0 | MIT |
| [fsevents](https://www.npmjs.com/package/fsevents/v/2.3.3) | 2.3.3 | MIT |
| [lightningcss](https://www.npmjs.com/package/lightningcss/v/1.33.0) | 1.33.0 | MPL-2.0 |
| [lightningcss-android-arm64](https://www.npmjs.com/package/lightningcss-android-arm64/v/1.33.0) | 1.33.0 | MPL-2.0 |
| [lightningcss-darwin-arm64](https://www.npmjs.com/package/lightningcss-darwin-arm64/v/1.33.0) | 1.33.0 | MPL-2.0 |
| [lightningcss-darwin-x64](https://www.npmjs.com/package/lightningcss-darwin-x64/v/1.33.0) | 1.33.0 | MPL-2.0 |
| [lightningcss-freebsd-x64](https://www.npmjs.com/package/lightningcss-freebsd-x64/v/1.33.0) | 1.33.0 | MPL-2.0 |
| [lightningcss-linux-arm-gnueabihf](https://www.npmjs.com/package/lightningcss-linux-arm-gnueabihf/v/1.33.0) | 1.33.0 | MPL-2.0 |
| [lightningcss-linux-arm64-gnu](https://www.npmjs.com/package/lightningcss-linux-arm64-gnu/v/1.33.0) | 1.33.0 | MPL-2.0 |
| [lightningcss-linux-arm64-musl](https://www.npmjs.com/package/lightningcss-linux-arm64-musl/v/1.33.0) | 1.33.0 | MPL-2.0 |
| [lightningcss-linux-x64-gnu](https://www.npmjs.com/package/lightningcss-linux-x64-gnu/v/1.33.0) | 1.33.0 | MPL-2.0 |
| [lightningcss-linux-x64-musl](https://www.npmjs.com/package/lightningcss-linux-x64-musl/v/1.33.0) | 1.33.0 | MPL-2.0 |
| [lightningcss-win32-arm64-msvc](https://www.npmjs.com/package/lightningcss-win32-arm64-msvc/v/1.33.0) | 1.33.0 | MPL-2.0 |
| [lightningcss-win32-x64-msvc](https://www.npmjs.com/package/lightningcss-win32-x64-msvc/v/1.33.0) | 1.33.0 | MPL-2.0 |
| [nanoid](https://www.npmjs.com/package/nanoid/v/3.3.18) | 3.3.18 | MIT |
| [picocolors](https://www.npmjs.com/package/picocolors/v/1.1.1) | 1.1.1 | ISC |
| [picomatch](https://www.npmjs.com/package/picomatch/v/4.0.7) | 4.0.7 | MIT |
| [postcss](https://www.npmjs.com/package/postcss/v/8.5.26) | 8.5.26 | MIT |
| [rolldown](https://www.npmjs.com/package/rolldown/v/1.2.6) | 1.2.6 | MIT |
| [source-map-js](https://www.npmjs.com/package/source-map-js/v/1.2.1) | 1.2.1 | BSD-3-Clause |
| [tinyglobby](https://www.npmjs.com/package/tinyglobby/v/0.2.17) | 0.2.17 | MIT |
| [vite](https://www.npmjs.com/package/vite/v/8.2.2) | 8.2.2 | MIT |

Build images also install tools outside these application lockfiles: npm 12.0.2
with brace-expansion 5.0.9, ip-address 10.3.1, and tar 7.5.21 security overrides.
Their own package notices must be retained if those build environments are
redistributed. Caddy/Go modules and browser/OS packages are discussed separately
in the top-level notices.
