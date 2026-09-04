from __future__ import annotations

import asyncio
import hmac
import re
from contextlib import suppress
from typing import cast
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Response
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Locator, Page, Route, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel, Field

from app.config import get_settings
from app.imports.url_security import UnsafeURL, validate_http_url_shape

renderer_app = FastAPI(title="Isolierter URL-Renderer", docs_url=None, redoc_url=None)
render_slots = asyncio.Semaphore(2)
MAX_RENDER_REQUESTS = 500
MAX_REDIRECTS_PER_REQUEST = 10
MAX_RENDERED_PDF_BYTES = 50 * 1024 * 1024
EGRESS_PROXY_HOST = "egress"
EGRESS_PROXY_PORT = 8888
CONSENT_BUTTON_SELECTORS = (
    "#onetrust-accept-btn-handler",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "#didomi-notice-agree-button",
    "#cmpwelcomebtnyes",
    "button[data-testid='uc-accept-all-button']",
    "button[data-testid='uc-accept-all']",
    ".qc-cmp2-summary-buttons button[mode='primary']",
)
CONSENT_BUTTON_NAME = re.compile(
    r"^(?:"
    r"Einwilligen und weiter|Alle(?: Cookies)? akzeptieren|Alles akzeptieren|"
    r"Akzeptieren und weiter|Accept all(?: cookies)?|Allow all|Agree and continue|"
    r"I agree|Tout accepter|Accepter tout|Alles accepteren|Accetta tutto|"
    r"Aceptar todo|Aceptar todas"
    r")$",
    re.IGNORECASE,
)
FOCUSED_CONTENT_CSS = """
@page { size: A4; margin: 10mm; }
html, body { background: white !important; color: #18181b !important; }
body { margin: 0 !important; overflow: visible !important; }
body > :not([data-recipe-render-root]) { display: none !important; }
[data-recipe-render-root] header,
[data-recipe-render-root] nav,
[data-recipe-render-root] footer,
[data-recipe-render-root] aside,
[data-recipe-render-root] [role="banner"],
[data-recipe-render-root] [role="navigation"],
[data-recipe-render-root] [role="contentinfo"],
[data-recipe-render-root] [role="dialog"],
[data-recipe-render-root] iframe,
[data-recipe-render-root] video,
[data-recipe-render-root] [class*="advertisement" i],
[data-recipe-render-root] [class*="ad-container" i],
[data-recipe-render-root] [id*="advertisement" i] { display: none !important; }
[data-recipe-render="structured"] {
  box-sizing: border-box;
  display: block !important;
  font-family: Arial, sans-serif;
  line-height: 1.45;
  margin: 0 auto !important;
  max-width: 900px;
  padding: 0 !important;
}
[data-recipe-render="structured"] h1 { font-size: 28px; line-height: 1.15; }
[data-recipe-render="structured"] h2 { font-size: 20px; margin-top: 24px; }
[data-recipe-render="structured"] img {
  display: block;
  max-height: 110mm;
  max-width: 100%;
  object-fit: contain;
}
[data-recipe-render="structured"] li { break-inside: avoid; margin-bottom: 4px; }
"""
FOCUS_RECIPE_SCRIPT = """
() => {
  const typeIncludesRecipe = (value) => {
    const type = value && value['@type'];
    return type === 'Recipe' || (Array.isArray(type) && type.includes('Recipe'));
  };
  const expanded = [];
  for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const parsed = JSON.parse(script.textContent || 'null');
      const values = Array.isArray(parsed) ? parsed : [parsed];
      for (const value of values) {
        if (value && Array.isArray(value['@graph'])) expanded.push(...value['@graph']);
        else expanded.push(value);
      }
    } catch (_error) {
      // Invalid JSON-LD is ignored; the semantic main-content fallback remains.
    }
  }
  const recipe = expanded.find(typeIncludesRecipe);
  const text = (value) => typeof value === 'string' ? value.trim() : '';
  const imageUrl = (value) => {
    if (typeof value === 'string') return value;
    if (Array.isArray(value)) {
      for (const item of value) {
        const found = imageUrl(item);
        if (found) return found;
      }
      return '';
    }
    if (value && typeof value === 'object') {
      return text(value.url) || text(value.contentUrl);
    }
    return '';
  };
  const instructionTexts = (value) => {
    if (typeof value === 'string') return [value.trim()].filter(Boolean);
    if (Array.isArray(value)) return value.flatMap(instructionTexts);
    if (!value || typeof value !== 'object') return [];
    const own = text(value.text) || text(value.name);
    const nested = instructionTexts(value.itemListElement || value.steps);
    return own ? [own, ...nested] : nested;
  };
  const addText = (parent, tag, value) => {
    const clean = text(value);
    if (!clean) return null;
    const element = document.createElement(tag);
    element.textContent = clean;
    parent.appendChild(element);
    return element;
  };

  if (recipe && text(recipe.name)) {
    const main = document.createElement('main');
    main.dataset.recipeRender = 'structured';
    main.dataset.recipeRenderRoot = '';
    addText(main, 'h1', recipe.name);
    const loadedImages = Array.from(document.querySelectorAll('main img, article img'))
      .filter((image) => image.complete && image.naturalWidth >= 300 && image.naturalHeight >= 200)
      .filter((image) => !/(?:logo|icon|pinterest|avatar)/i.test(image.alt || ''))
      .sort((left, right) =>
        (right.naturalWidth * right.naturalHeight) - (left.naturalWidth * left.naturalHeight));
    const loadedImage = loadedImages[0];
    const sourceImage = loadedImage ? (loadedImage.currentSrc || loadedImage.src) : imageUrl(recipe.image);
    if (loadedImage && sourceImage) {
      loadedImage.removeAttribute('srcset');
      loadedImage.removeAttribute('sizes');
      loadedImage.removeAttribute('loading');
      loadedImage.src = sourceImage;
      loadedImage.alt = text(recipe.name);
      main.appendChild(loadedImage);
    } else if (sourceImage) {
      const fallbackImage = document.createElement('img');
      fallbackImage.src = sourceImage;
      fallbackImage.alt = text(recipe.name);
      main.appendChild(fallbackImage);
    }
    addText(main, 'p', recipe.description);
    const facts = [
      ['Portionen', recipe.recipeYield],
      ['Vorbereitung', recipe.prepTime],
      ['Kochen', recipe.cookTime],
      ['Gesamtzeit', recipe.totalTime],
    ].filter((item) => text(item[1]));
    if (facts.length) {
      const list = document.createElement('ul');
      for (const [label, value] of facts) addText(list, 'li', `${label}: ${text(value)}`);
      main.appendChild(list);
    }
    const ingredients = Array.isArray(recipe.recipeIngredient) ? recipe.recipeIngredient : [];
    if (ingredients.length) {
      addText(main, 'h2', 'Zutaten');
      const list = document.createElement('ul');
      for (const ingredient of ingredients) addText(list, 'li', ingredient);
      main.appendChild(list);
    }
    const instructions = instructionTexts(recipe.recipeInstructions);
    if (instructions.length) {
      addText(main, 'h2', 'Zubereitung');
      const list = document.createElement('ol');
      for (const instruction of instructions) addText(list, 'li', instruction);
      main.appendChild(list);
    }
    addText(main, 'p', `Quelle: ${location.href}`);
    document.body.replaceChildren(main);
    document.documentElement.style.overflow = 'visible';
    document.body.style.overflow = 'visible';
    return 'structured';
  }

  const candidates = Array.from(document.querySelectorAll('main, article'))
    .filter((element) => (element.innerText || '').trim().length >= 200)
    .sort((left, right) => (right.innerText || '').length - (left.innerText || '').length);
  const target = candidates[0];
  if (!target) return null;
  let root = target;
  while (root.parentElement && root.parentElement !== document.body) root = root.parentElement;
  root.dataset.recipeRenderRoot = '';
  target.dataset.recipeRender = 'semantic';
  document.documentElement.style.overflow = 'visible';
  document.body.style.overflow = 'visible';
  return 'semantic';
}
"""


class RenderRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)


def _configured_egress_proxy(raw_url: str) -> str | None:
    if raw_url != raw_url.strip() or any(character.isspace() for character in raw_url):
        return None
    parsed = urlparse(raw_url)
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "http"
        or parsed.hostname != EGRESS_PROXY_HOST
        or port != EGRESS_PROXY_PORT
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None
    return f"http://{EGRESS_PROXY_HOST}:{EGRESS_PROXY_PORT}"


async def _click_visible(locator: Locator) -> bool:
    """Click at most one visible consent control represented by a locator."""

    try:
        count = min(await locator.count(), 10)
        for index in range(count):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                await candidate.click(timeout=3_000)
                return True
    except PlaywrightError:
        # Consent frames are often removed while they are being inspected.
        return False
    return False


async def _dismiss_cookie_dialog(page: Page) -> bool:
    """Accept a recognized CMP dialog in the main document or a child frame.

    The browser context is short-lived and isolated per render, so no consent
    state is retained. Matching is deliberately limited to established CMP
    selectors and explicit accept-all labels to avoid clicking normal page UI.
    """

    for attempt in range(4):
        frames: list[Frame] = list(page.frames)
        for frame in frames:
            for selector in CONSENT_BUTTON_SELECTORS:
                if await _click_visible(frame.locator(selector)):
                    await page.wait_for_timeout(750)
                    with suppress(PlaywrightTimeoutError):
                        await page.wait_for_load_state("networkidle", timeout=5_000)
                    return True
            if await _click_visible(
                frame.get_by_role("button", name=CONSENT_BUTTON_NAME, exact=True)
            ):
                await page.wait_for_timeout(750)
                with suppress(PlaywrightTimeoutError):
                    await page.wait_for_load_state("networkidle", timeout=5_000)
                return True
        if attempt < 3:
            await page.wait_for_timeout(500)
    return False


async def _focus_recipe_content(page: Page) -> str | None:
    """Reduce a page to structured Recipe JSON-LD or its semantic main area."""

    mode = cast(str | None, await page.evaluate(FOCUS_RECIPE_SCRIPT))
    if not mode:
        return None
    await page.add_style_tag(content=FOCUSED_CONTENT_CSS)
    with suppress(PlaywrightTimeoutError):
        await page.wait_for_function(
            "() => Array.from(document.images).every((image) => "
            "image.complete && image.naturalWidth > 0)",
            timeout=10_000,
        )
    return mode


@renderer_app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@renderer_app.post("/render/pdf")
async def render_pdf(
    payload: RenderRequest, authorization: str | None = Header(default=None)
) -> Response:
    settings = get_settings()
    expected = f"Bearer {settings.renderer_token}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Nicht autorisiert")
    proxy_url = _configured_egress_proxy(settings.renderer_proxy_url)
    if not proxy_url:
        raise HTTPException(
            status_code=503,
            detail="Der sichere Egress-Proxy ist nicht konfiguriert",
        )
    try:
        validate_http_url_shape(payload.url)
    except UnsafeURL as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    async with render_slots, async_playwright() as playwright:
        browser_args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-background-networking",
            "--disable-features=InterestFeedContentSuggestions,OptimizationHints",
            "--disable-quic",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--proxy-bypass-list=<-loopback>",
            f"--proxy-server={proxy_url}",
        ]
        browser = await playwright.chromium.launch(args=browser_args)
        context = await browser.new_context(
            accept_downloads=False,
            java_script_enabled=True,
            service_workers="block",
        )
        page = await context.new_page()
        request_count = 0

        async def guard(route: Route) -> None:
            nonlocal request_count
            request_count += 1
            if request_count > MAX_RENDER_REQUESTS:
                await route.abort()
                return
            request = route.request
            if request.resource_type in {"websocket", "media"}:
                await route.abort()
                return
            redirected_from = getattr(request, "redirected_from", None)
            redirect_count = 0
            while redirected_from is not None:
                redirect_count += 1
                if redirect_count > MAX_REDIRECTS_PER_REQUEST:
                    await route.abort()
                    return
                redirected_from = getattr(redirected_from, "redirected_from", None)
            try:
                validate_http_url_shape(request.url)
            except UnsafeURL:
                await route.abort()
                return
            await route.continue_()

        await context.route("**/*", guard)
        try:
            try:
                navigation = await page.goto(
                    payload.url,
                    wait_until="networkidle",
                    timeout=60_000,
                )
                if navigation is None or not navigation.ok:
                    raise HTTPException(
                        status_code=422,
                        detail="Die Webseite wurde beim sicheren Verbindungsaufbau abgelehnt",
                    )
                await _dismiss_cookie_dialog(page)
                await _focus_recipe_content(page)
                pdf = await page.pdf(
                    format="A4",
                    print_background=True,
                    margin={
                        "top": "10mm",
                        "right": "10mm",
                        "bottom": "10mm",
                        "left": "10mm",
                    },
                )
            except PlaywrightTimeoutError as exc:
                raise HTTPException(
                    status_code=504,
                    detail="Die Webseite hat nicht rechtzeitig vollständig geladen",
                ) from exc
            except PlaywrightError as exc:
                raise HTTPException(
                    status_code=422,
                    detail="Die Webseite konnte nicht sicher gerendert werden",
                ) from exc
            if len(pdf) > MAX_RENDERED_PDF_BYTES:
                raise HTTPException(status_code=413, detail="Die gerenderte Seite ist zu groß")
            return Response(pdf, media_type="application/pdf")
        finally:
            await browser.close()
