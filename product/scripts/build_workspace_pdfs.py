"""Build PDF originals from the existing demonstration fixture text."""
from pathlib import Path
from io import BytesIO
import sys

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'product/src'))
from carerelay.discharge_service import LETTER, REHAB, APPOINTMENT, TITLES


def main():
    output = ROOT/'product/src/carerelay/assets/demo'
    output.mkdir(parents=True, exist_ok=True)
    body = ParagraphStyle('body', fontName='Helvetica', fontSize=12, leading=20, textColor=colors.HexColor('#203044'), spaceAfter=16)
    title = ParagraphStyle('title', parent=body, fontName='Helvetica-Bold', fontSize=23, leading=29, spaceAfter=24)
    small = ParagraphStyle('small', parent=body, fontSize=10, leading=16, textColor=colors.HexColor('#53677b'))
    def frame(canvas, doc):
        canvas.setFillColor(colors.HexColor('#123448'))
        canvas.rect(0, 770, 595, 72, stroke=0, fill=1)
        canvas.setFont('Helvetica-Bold', 14)
        canvas.setFillColor(colors.white)
        canvas.drawString(48, 801, 'AfterWard | Continuity of care')
        canvas.setFillColor(colors.HexColor('#53677b'))
        canvas.setFont('Helvetica', 9)
        canvas.drawString(48, 38, 'Demonstration document - Giorgio Rinaldi')
        canvas.drawRightString(547, 38, f'Page {doc.page}')
    for identifier, text in (('letter', LETTER), ('rehab', REHAB), ('appointment', APPOINTMENT)):
        flow = [Paragraph(TITLES[identifier], title), Paragraph('Giorgio Rinaldi | Medicine', small), Spacer(1, 24)]
        flow.extend(Paragraph(part.replace('\n','<br/>'), body) for part in text.strip().split('\n\n'))
        SimpleDocTemplate(str(output/f'{identifier}.pdf'), pagesize=(595,842), leftMargin=48, rightMargin=48,
            topMargin=108, bottomMargin=65, title=TITLES[identifier], author='AfterWard').build(flow, onFirstPage=frame)
    with pdfium.PdfDocument(output/'letter.pdf') as source:
        page = source[0]
        bitmap = page.render(scale=2)
        image = bitmap.to_pil().copy()
        bitmap.close()
        page.close()
    scan = canvas.Canvas(str(output/'discharge-scan.pdf'), pagesize=(595,842))
    scan.setTitle('Scanned demonstration discharge letter')
    scan.setAuthor('AfterWard')
    scan.drawImage(ImageReader(image), 0, 0, 595, 842)
    scan.save()
    print('Built three demonstration PDFs and a scanned letter.')


if __name__ == '__main__':
    main()
