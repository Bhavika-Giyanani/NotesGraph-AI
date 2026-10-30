"""Render structured notes to Markdown and PDF."""
import io
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import HRFlowable, ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer

from .config import FONTS_DIR

KIND_LABEL = {
    "important_question": "Likely question",
    "important_topic": "Focus topic",
    "professor_hint": "Professor's hint",
}


# ---------------------------------------------------------------- Markdown
def notes_to_markdown(n: dict) -> str:
    out = [f"# {n.get('title', 'Lecture notes')}"]
    if n.get("subject"):
        out.append(f"*{n['subject']}*")
    out += ["", "## Summary", n.get("summary", ""), "", "## Core explanations"]
    for c in n.get("core_explanations", []):
        out += ["", f"### {c['topic']}", c["explanation"]]
        out += [f"- {p}" for p in c.get("key_points", [])]
    out += ["", "## Announcements"]
    items = n.get("announcements", [])
    out += [f"- {a['text']}" + (f" (**{a['deadline']}**)" if a.get("deadline") else "") for a in items] or ["_None._"]
    out += ["", "## Exam-relevant points"]
    items = n.get("exam_points", [])
    out += [f"- **{KIND_LABEL.get(e['kind'], e['kind'])}:** {e['point']}" for e in items] or ["_None._"]
    out += ["", "## Off-topic remarks"]
    items = n.get("off_topic", [])
    out += [f"- {o['summary']}" for o in items] or ["_None._"]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------- PDF
def _fonts() -> tuple:
    """Use Noto Sans from ./fonts if present (full Unicode), else built-in Helvetica."""
    reg, bold = FONTS_DIR / "NotoSans-Regular.ttf", FONTS_DIR / "NotoSans-Bold.ttf"
    if reg.exists() and bold.exists():
        pdfmetrics.registerFont(TTFont("Noto", str(reg)))
        pdfmetrics.registerFont(TTFont("Noto-Bold", str(bold)))
        return "Noto", "Noto-Bold"
    return "Helvetica", "Helvetica-Bold"


def _p(text: str) -> str:
    return escape(text or "").replace("\n", "<br/>")


def notes_to_pdf(n: dict) -> bytes:
    body, bold = _fonts()
    base = getSampleStyleSheet()
    s = {
        "title": ParagraphStyle("t", parent=base["Title"], fontName=bold, fontSize=22, leading=26, alignment=TA_LEFT, spaceAfter=2),
        "sub": ParagraphStyle("sub", parent=base["Normal"], fontName=body, fontSize=10, textColor=colors.HexColor("#5b6472"), spaceAfter=8),
        "h1": ParagraphStyle("h1", parent=base["Heading2"], fontName=bold, fontSize=14, leading=18, spaceBefore=14, spaceAfter=6),
        "h2": ParagraphStyle("h2", parent=base["Heading3"], fontName=bold, fontSize=11.5, leading=15, spaceBefore=8, spaceAfter=2),
        "p": ParagraphStyle("p", parent=base["BodyText"], fontName=body, fontSize=10, leading=14.5, spaceAfter=4),
        "li": ParagraphStyle("li", parent=base["BodyText"], fontName=body, fontSize=10, leading=14),
    }
    accent = {
        "core": colors.HexColor("#2f5fd0"),
        "ann": colors.HexColor("#c98a00"),
        "exam": colors.HexColor("#d23b3b"),
        "off": colors.HexColor("#2b8a7e"),
    }

    def heading(text: str, key: str):
        return [Paragraph(f'<font color="{accent[key].hexval().replace("0x", "#")}">{_p(text)}</font>', s["h1"]),
                HRFlowable(width="100%", thickness=1.2, color=accent[key], spaceAfter=6)]

    def bullets(items):
        return ListFlowable([ListItem(Paragraph(t, s["li"]), leftIndent=12) for t in items],
                            bulletType="bullet", start="•", leftIndent=12, bulletFontName=body)

    story = [Paragraph(_p(n.get("title", "Lecture notes")), s["title"])]
    if n.get("subject"):
        story.append(Paragraph(_p(n["subject"]), s["sub"]))
    story.append(Paragraph(_p(n.get("summary", "")), s["p"]))

    story += heading("Core explanations", "core")
    for c in n.get("core_explanations", []):
        story.append(Paragraph(_p(c["topic"]), s["h2"]))
        story.append(Paragraph(_p(c["explanation"]), s["p"]))
        if c.get("key_points"):
            story.append(bullets([_p(k) for k in c["key_points"]]))

    story += heading("Announcements", "ann")
    ann = n.get("announcements", [])
    story.append(bullets([_p(a["text"]) + (f" <b>({_p(a['deadline'])})</b>" if a.get("deadline") else "") for a in ann])
                 if ann else Paragraph("None.", s["p"]))

    story += heading("Exam-relevant points", "exam")
    ex = n.get("exam_points", [])
    story.append(bullets([f"<b>{_p(KIND_LABEL.get(e['kind'], e['kind']))}:</b> {_p(e['point'])}" for e in ex])
                 if ex else Paragraph("None.", s["p"]))

    story += heading("Off-topic remarks", "off")
    off = n.get("off_topic", [])
    story.append(bullets([_p(o["summary"]) for o in off]) if off else Paragraph("None.", s["p"]))
    story.append(Spacer(1, 6 * mm))

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
                      title=n.get("title", "Lecture notes")).build(story)
    return buf.getvalue()
