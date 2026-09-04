# Third-party notices

The project's own source code is licensed under [MIT](LICENSE). Third-party
software retains its respective copyrights, licenses, notices, and source-code
obligations. This document does not relicense dependencies or user-imported content.

This repository distributes source and build instructions. Dependencies are
downloaded by the package managers or container build. Their complete license
texts are supplied by the upstream packages. A source-only publication does not
include those downloaded binaries. Distributing a built container, wheel bundle,
or executable requires a separate check of the exact artifacts being distributed.

The [dependency inventory](docs/DEPENDENCY_LICENSES.md) lists every entry in the
Python and npm lockfiles reviewed for this release, with version-specific source
links. It records package metadata; it is not a complete binary SBOM. Native
libraries, browser builds, operating-system packages, and Go modules can add
licenses beyond a package's top-level declaration.

## Native image support: Pillow and pillow-heif

Pillow 12.3.0 declares MIT-CMU. Its wheels contain additional libraries and license
texts that must be retained when redistributing them.

The Python wrapper in [pillow-heif 1.5.0](https://pypi.org/project/pillow-heif/1.5.0/)
is BSD-3-Clause, but that is **not the license of the complete binary wheel**.
The upstream [bundled-library notice](https://github.com/bigcat88/pillow_heif/blob/v1.5.0/LICENSES_bundled.txt)
identifies GPLv2 binary wheels because of x265, with LGPLv3 libheif and libde265
components. The pinned Linux CPython 3.12 x86-64 wheel includes these notices and
libraries. Other platforms must be checked individually.

Retain all bundled notices. Before distributing binaries containing these
components, satisfy the applicable GPL/LGPL source and relinking requirements for
the actual library versions and build. Upstream's bundled notice contains older
source-version links; a link to that notice alone is not a complete corresponding
source delivery. This repository does not claim that its MIT license covers a
built image containing those binaries.

## PostgreSQL adapter and task processing

[psycopg and psycopg-binary 3.3.4](https://pypi.org/project/psycopg/3.3.4/) declare
LGPL-3.0-only. The binary package also bundles native libraries with their own
notices. [Dramatiq 2.2.0](https://pypi.org/project/dramatiq/2.2.0/) is LGPL-3.0-or-later.
Retain their license files and fulfill the applicable LGPL obligations when
redistributing an application bundle, including recipients' ability to replace
or modify the LGPL components. Listing a dependency does not change its license.

## PDF support: pypdfium2 and PDFium

[pypdfium2 5.13.0](https://pypi.org/project/pypdfium2/5.13.0/) offers its Python wrapper
under Apache-2.0 or BSD-3-Clause. Distributed PDFium binaries include PDFium and
additional components under their respective licenses. Complete notices and
license texts for the installed binary are distributed inside the package; the
application Dockerfile copies the full installed environment and retains them.
Redistributors must keep those files with the binaries.

## MPL components and development tools

certifi uses MPL-2.0. orjson declares MPL-2.0 AND (Apache-2.0 OR MIT). Lightning CSS
(a frontend build dependency), pathspec, and pytest-base-url also use MPL-2.0.
The MPL's obligations apply to its covered files; this notice does not assert that
the entire application becomes MPL-licensed. Preserve notices and provide the
required covered source when distributing those components or modifications.

The development-only text-unidecode dependency offers Artistic/GPL licensing;
its exact terms are supplied in its package. Vite and other build tools bundle
additional notices in their published packages. The frontend uses the build
output; the project does not redistribute node_modules in this source repository.

## Containers and services

- The **Python Redis client** 6.4.0 is MIT. The separate **Redis server** 8.10.1
  image is governed by Redis's choice of AGPLv3, RSALv2, or SSPLv1; see
  [Redis licensing](https://redis.io/legal/licenses/). These are distinct products.
  Select and comply with the appropriate server license for your use/distribution.
- PostgreSQL uses the [PostgreSQL License](https://www.postgresql.org/about/licence/).
- Caddy is Apache-2.0 and includes Go dependencies under their respective licenses.
  The custom Caddy build collects the downloaded modules' license/notice files
  and module versions into `/licenses/go-modules`, alongside Caddy's license.
  Review the resulting image before redistributing it; the inventory is not a
  substitute for source obligations or notices embedded under unusual filenames.
- Python, Go, Node.js, Chromium/Playwright, Alpine/Debian packages, CA certificates,
  and timezone data have their own notices. The browser and OS components are
  not exhaustively enumerated in the package-lock inventory.

Noncommercial use and private hosting do not waive license conditions. Keep
upstream copyright and license texts, notices, and any required source with
distributed artifacts; do not describe a complete application image as solely
MIT-licensed on the basis of this repository's top-level license.
