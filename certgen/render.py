"""ReportLab certificate renderer. Pure: payload in, PDF bytes out.

ReportLab rather than WeasyPrint or LaTeX because it is pure Python, so a clone runs with
no system packages installed.
"""

from __future__ import annotations

import io
from typing import Any

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


def render_certificate(
    payload: dict[str, Any], certificate_id: str, template_id: str
) -> bytes:
    """Render one certificate. Must not depend on anything outside `payload`."""
    width, height = landscape(A4)
    buf = io.BytesIO()
    # invariant=1 pins the timestamp and document id, so the same payload renders the same
    # bytes and a certificate digest identifies its content rather than the moment it was made.
    pdf = canvas.Canvas(buf, pagesize=landscape(A4), invariant=1)
    pdf.setTitle(f"Certificate {certificate_id}")

    margin = 15 * mm
    pdf.setLineWidth(2.5)
    pdf.rect(margin, margin, width - 2 * margin, height - 2 * margin)
    pdf.setLineWidth(0.8)
    pdf.rect(margin + 4 * mm, margin + 4 * mm, width - 2 * margin - 8 * mm, height - 2 * margin - 8 * mm)

    baseline = margin + (height - 2 * margin) * 0.5
    # Room inside the inner border, with breathing space on both sides.
    max_width = width - 2 * margin - 28 * mm

    pdf.setFillGray(0.35)
    pdf.setFont("Helvetica", 13)
    pdf.drawCentredString(width / 2, baseline + 38 * mm, "CERTIFICATE OF COMPLETION")

    pdf.setFillGray(0.0)
    name = _text(payload.get("name"), "—")
    pdf.setFont("Helvetica-Bold", _fit(name, "Helvetica-Bold", 34, max_width))
    pdf.drawCentredString(width / 2, baseline + 12 * mm, name)

    pdf.setFont("Helvetica", 13)
    pdf.drawCentredString(width / 2, baseline - 2 * mm, "has successfully completed")

    course = _text(payload.get("course"), "—")
    pdf.setFont("Helvetica-Bold", _fit(course, "Helvetica-Bold", 20, max_width))
    pdf.drawCentredString(width / 2, baseline - 18 * mm, course)

    pdf.setFillGray(0.35)
    pdf.setFont("Helvetica", 11)
    pdf.drawString(margin + 12 * mm, margin + 10 * mm, f"Date: {_text(payload.get('date'), '—')}")
    pdf.drawRightString(
        width - margin - 12 * mm, margin + 10 * mm, f"{certificate_id}  ·  {template_id}"
    )

    pdf.showPage()
    pdf.save()
    return buf.getvalue()


def _fit(text: str, font: str, size: float, max_width: float) -> float:
    """The largest font size up to `size` at which `text` fits inside `max_width`."""
    natural = stringWidth(text, font, size)
    return size if natural <= max_width else size * max_width / natural


def _text(value: Any, fallback: str) -> str:
    text = "" if value is None else str(value).strip()
    return text or fallback
