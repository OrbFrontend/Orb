"""API documentation with local assets and no external validator or fonts."""

from fastapi import APIRouter
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.responses import HTMLResponse

router = APIRouter(include_in_schema=False)
_ASSETS = "/static/vendor/api-docs"
_FAVICON = "/static/favicon.svg"


def _local_page(page: HTMLResponse) -> HTMLResponse:
    # Keep future vendor changes from silently adding an external dependency.
    page.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline' blob:; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "font-src 'self' data:; worker-src 'self' blob:; object-src 'none'; base-uri 'self'"
    )
    page.headers["Referrer-Policy"] = "no-referrer"
    return page


@router.get("/docs")
async def swagger_ui() -> HTMLResponse:
    return _local_page(
        get_swagger_ui_html(
            openapi_url="/openapi.json",
            title="Orb - Swagger UI",
            swagger_js_url=f"{_ASSETS}/swagger-ui-bundle.js",
            swagger_css_url=f"{_ASSETS}/swagger-ui.css",
            swagger_favicon_url=_FAVICON,
            oauth2_redirect_url="/docs/oauth2-redirect",
            swagger_ui_parameters={"validatorUrl": None},
        )
    )


@router.get("/docs/oauth2-redirect")
async def swagger_redirect() -> HTMLResponse:
    return _local_page(get_swagger_ui_oauth2_redirect_html())


@router.get("/redoc")
async def redoc() -> HTMLResponse:
    return _local_page(
        get_redoc_html(
            openapi_url="/openapi.json",
            title="Orb - ReDoc",
            redoc_js_url=f"{_ASSETS}/redoc.standalone.js",
            redoc_favicon_url=_FAVICON,
            with_google_fonts=False,
        )
    )
