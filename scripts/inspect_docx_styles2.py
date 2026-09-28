import docx
from docx.oxml.ns import qn
from lxml import etree

PATH = "reference/AccuBreath_Due_Diligence_Report.docx"
d = docx.Document(PATH)


def x(elem):
    return etree.tostring(elem, pretty_print=True).decode()


t = d.tables[0]
tbl = t._tbl
tblPr = tbl.find(qn("w:tblPr"))
tblBorders = tblPr.find(qn("w:tblBorders"))
print("--- Full tblBorders ---")
print(x(tblBorders))

cell = t.rows[0].cells[0]
tcPr = cell._tc.find(qn("w:tcPr"))
print("--- Full tcPr (header cell) ---")
print(x(tcPr))

# A body (non-header) row cell, if the table has >1 row
if len(t.rows) > 1:
    body_cell = t.rows[1].cells[0]
    body_tcPr = body_cell._tc.find(qn("w:tcPr"))
    print("--- Full tcPr (body cell) ---")
    print(x(body_tcPr) if body_tcPr is not None else "(none -- no direct cell formatting)")

print("--- docDefaults ---")
styles_element = d.styles.element
docDefaults = styles_element.find(qn("w:docDefaults"))
print(x(docDefaults) if docDefaults is not None else "(none)")

print("--- Normal style pPr/rPr ---")
normal = d.styles["Normal"]
print(x(normal.element))

print("--- Heading 1 style element ---")
h1 = d.styles["Heading 1"]
print(x(h1.element))

print("--- Title style element ---")
title = d.styles["Title"]
print(x(title.element))
