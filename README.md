# Recipes

A self-hosted recipe manager for households and small groups. Keep a shared collection, import recipes from photos, PDFs and websites, and use it on your phone as an installable web app.

Built with FastAPI, PostgreSQL, Redis/Dramatiq and Caddy. Available in English, German, Spanish, Hindi and Simplified Chinese.

## Features

- Cooking and baking recipes with serving adjustments, nutrition, photos and version history.
- Categories, tags, full-text search, search synonyms and personal favorites.
- Optional AI imports with drafts to review before saving, plus optional image generation.
- JSON recipe packages, PDF export, printing and revocable recipe-sharing links.
- Shared recipes and comments, private notes, and administrator-managed accounts.
- Full backups and validated restores, including original files.

All active members can access the shared recipe collection. There is no public registration. The PWA caches app assets, not private recipes or media.

## Quick start

Requires Docker with Compose v2 and `make`.

```bash
git clone https://github.com/phinaldoo/recipes.git
cd recipes
cp .env.example .env
```

Set **`APP_SECRET_KEY`**, **`RENDERER_TOKEN`** and **`POSTGRES_PASSWORD`** in `.env` to separate random values. Generate each with `openssl rand -hex 32`.

```bash
make up
docker compose exec app python -m app.cli users create \
  --email admin@example.com --display-name "Admin" --role admin
```

Enter a password when prompted, then open [localhost:8080](http://localhost:8080). See [.env.example](.env.example) for upload limits, storage quotas, retention and other settings.

## AI imports

AI is optional: manual recipe management works without an API key. To enable imports, configure a provider that supports the Responses API with structured outputs:

```dotenv
AI_API_KEY=your-api-key
AI_BASE_URL=https://your-provider.example/v1
AI_EXTRACTION_MODEL=your-extraction-model
```

Apply changes with `make restart`. Review the import drafts and confirm which recipes to save. Website imports run through an isolated browser and egress proxy.

For image generation, also set `AI_IMAGE_GENERATION_ENABLED=true` and choose `AI_IMAGE_MODEL`. API keys stay on the server. AI processing sends source content or recipe data to your configured provider.

## Running your instance

| Command | What it does |
| --- | --- |
| `make up` | Build and start the stack; wait for healthy services. |
| `make restart` | Recreate services and apply configuration changes. |
| `make update` | Refresh images, back up the running app, then replace services. |
| `make down` | Stop the stack and preserve data volumes. |

Create additional accounts with the same CLI command, omitting `--role admin` for members. For password resets, roles and deactivation, run:

```bash
docker compose exec app python -m app.cli users --help
```

Administrators can create backups and restore them from Settings. For scripted backups:

```bash
docker compose exec -T app python -m app.cli backups create
```

Backups include password hashes and original documents. Store encrypted copies outside the host and test restores on a disposable instance. Uploads and exports enforce size, concurrency and free-space limits; adjust the settings to match your storage.

### HTTPS deployment

After completing the quick-start configuration, point your domain at the server and update `.env`:

```dotenv
APP_ENV=production
APP_BASE_URL=https://recipes.example.com
APP_DOMAIN=recipes.example.com
ALLOWED_HOSTS=recipes.example.com
SESSION_COOKIE_SECURE=true
FORCE_HTTPS=true
PUBLIC_PORT=443
```

Run `make up PRODUCTION=1`. Use `PRODUCTION=1` with subsequent lifecycle commands too. Only Caddy exposes a host port. Allow inbound TCP 443 for HTTPS and certificate validation; certificates and application data use persistent volumes.

## Development

Use Python 3.12–3.14 and Node.js 24. Install the locked dependencies and build frontend assets before starting the app:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install --require-hashes -r requirements-dev.lock
npm ci --ignore-scripts
npm run build
npm test
```

Run `make assets` after frontend changes. The [CI workflow](.github/workflows/ci.yml) documents the test environment and commands for Python tests, type checks, coverage, dependency audits and browser tests. Integration and restore tests must use disposable PostgreSQL, Redis and application instances.

## License and content rights

The project's source code is [MIT-licensed](LICENSE). Dependencies retain their own licenses; see [third-party notices](THIRD_PARTY_NOTICES.md) and the [dependency inventory](docs/DEPENDENCY_LICENSES.md). Distributing built images or binaries may require additional notices and corresponding source.

This repository contains source and build instructions, not a recipe collection. Only import, send to AI services or share content you have the right to use.
