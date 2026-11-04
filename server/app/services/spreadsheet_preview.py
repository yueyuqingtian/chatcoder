"""plan-20261103-attachment-preview：只读表格区域，不执行公式或外部连接。"""
import csv
import io
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from fastapi import HTTPException
from app.services.preview_service import check_office, decode_text


def color(c):
    if c is None: return None
    if c.type == "rgb" and isinstance(c.rgb, str): return "#" + c.rgb[-6:]
    if c.type == "indexed":
        from openpyxl.styles.colors import COLOR_INDEXED
        if c.indexed < len(COLOR_INDEXED): return "#" + COLOR_INDEXED[c.indexed][-6:]
    return None


def display(value, fmt="General"):
    if value is None: return ""
    if isinstance(value, (datetime, date)): return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, bool): return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        import re
        section = fmt.split(";")[0]
        decimals = len(re.search(r"\.(0+)", section).group(1)) if re.search(r"\.(0+)", section) else 0
        if "%" in section: return f"{value * 100:.{decimals}f}%"
        symbol = next((x for x in ("¥", "$", "€", "£") if x in section), "")
        if symbol or "0.00" in section or "#,##0" in section:
            return symbol + (f"{value:,.{decimals}f}" if "," in section else f"{value:.{decimals}f}")
    return str(value)


@lru_cache(maxsize=4)
def load_book(filename, stamp, size):
    path = Path(filename)
    check_office(path)
    ext = path.suffix.lower()
    if ext not in {".xlsx", ".xls", ".csv", ".tsv"}:
        raise HTTPException(415,"此文件不是支持的表格格式")
    try:
        if ext == ".xlsx":
            import openpyxl
            return ("xlsx", openpyxl.load_workbook(path, data_only=False, keep_links=False),
                    openpyxl.load_workbook(path, data_only=True, keep_links=False))
        if ext == ".xls":
            import xlrd
            return ("xls", xlrd.open_workbook(path, formatting_info=True), None)
        rows = list(csv.reader(io.StringIO(decode_text(path.read_bytes())), delimiter="\t" if ext == ".tsv" else ","))
        return ("csv", rows, None)
    except HTTPException: raise
    except Exception:
        raise HTTPException(422, "表格已损坏、加密或格式不受支持")


def book_for(path):
    stat = path.stat()
    return load_book(str(path), stat.st_mtime_ns, stat.st_size)


def metadata(path):
    kind, book, _ = book_for(path)
    if kind == "xlsx": sheets = [{"name": s.title, "rows": s.max_row, "cols": s.max_column} for s in book.worksheets]
    elif kind == "xls": sheets = [{"name": s.name, "rows": s.nrows, "cols": s.ncols} for s in book.sheets()]
    else: sheets = [{"name": path.stem, "rows": len(book), "cols": max((len(r) for r in book), default=0)}]
    return {"sheets": sheets, "notice": "只读预览；图表、透视表及复杂条件格式可能未显示，公式使用文件保存的结果。" if kind != "csv" else ""}


def region(path, sheet, row, col, rows=200, cols=50):
    kind, book, cached = book_for(path)
    cells, merges, widths, heights = [], [], {}, {}
    if kind == "xlsx":
        if sheet not in book.sheetnames: raise HTTPException(404, "工作表不存在")
        ws, values = book[sheet], cached[sheet]
        for merged in ws.merged_cells.ranges:
            a, b, c, d = merged.min_row-1, merged.min_col-1, merged.max_row, merged.max_col
            if a < row+rows and c > row and b < col+cols and d > col: merges.append([a,b,c,d])
        anchors = {(m[0],m[1]) for m in merges}
        points = {(r,c) for r in range(row,min(row+rows,ws.max_row)) for c in range(col,min(col+cols,ws.max_column))} | anchors
        for r,c in sorted(points):
            cell = ws.cell(r+1,c+1)
            value = values.cell(r+1,c+1).value if cell.data_type == "f" else cell.value
            missing = cell.data_type == "f" and value is None
            style = {"fontFamily": cell.font.name, "fontSize": cell.font.sz, "fontWeight": "bold" if cell.font.b else "normal",
                     "fontStyle": "italic" if cell.font.i else "normal", "color": color(cell.font.color),
                     "backgroundColor": color(cell.fill.fgColor) if cell.fill.patternType == "solid" else None,
                     "textAlign": cell.alignment.horizontal if cell.alignment.horizontal in {"left","right","center","justify"} else None,
                     "verticalAlign": cell.alignment.vertical if cell.alignment.vertical in {"top","bottom"} else "middle",
                     "whiteSpace": "pre-wrap" if cell.alignment.wrap_text else "pre"}
            for side in ("top","right","bottom","left"):
                border = getattr(cell.border, side)
                if border and border.style: style["border"+side.title()] = "1px solid " + (color(border.color) or "#999999")
            cells.append({"row":r,"col":c,"text":str(cell.value)+"（无缓存结果）" if missing else display(value,cell.number_format),"style":{k:v for k,v in style.items() if v is not None}})
        from openpyxl.utils import get_column_letter
        for c in range(col,min(col+cols,ws.max_column)):
            dim = ws.column_dimensions.get(get_column_letter(c+1))
            widths[c] = max(32,min(600,(dim.width*7 if dim and dim.width else 96)))
        for r in range(row,min(row+rows,ws.max_row)):
            dim = ws.row_dimensions.get(r+1)
            heights[r] = max(26,min(300,dim.height*4/3 if dim and dim.height else 28))
    elif kind == "xls":
        import xlrd
        try: ws = book.sheet_by_name(sheet)
        except Exception: raise HTTPException(404, "工作表不存在")
        merges = [list(m) for m in ws.merged_cells if m[0]<row+rows and m[1]>row and m[2]<col+cols and m[3]>col]
        merges = [[a,c,b,d] for a,b,c,d in merges]
        points = {(r,c) for r in range(row,min(row+rows,ws.nrows)) for c in range(col,min(col+cols,ws.ncols))} | {(m[0],m[1]) for m in merges}
        def xcolor(index):
            rgb = book.colour_map.get(index)
            return "#%02x%02x%02x" % rgb if rgb else None
        for r,c in sorted(points):
            cell = ws.cell(r,c)
            xf = book.xf_list[cell.xf_index]; font = book.font_list[xf.font_index]
            value = cell.value
            if cell.ctype == xlrd.XL_CELL_DATE:
                try: value = xlrd.xldate.xldate_as_datetime(value,book.datemode)
                except ValueError: pass
            style = {"fontFamily":font.name,"fontSize":font.height/20,"fontWeight":"bold" if font.bold else "normal",
                     "fontStyle":"italic" if font.italic else "normal","color":xcolor(font.colour_index),
                     "backgroundColor":xcolor(xf.background.pattern_colour_index) if xf.background.fill_pattern else None,
                     "textAlign":{1:"left",2:"center",3:"right"}.get(xf.alignment.hor_align),
                     "whiteSpace":"pre-wrap" if xf.alignment.text_wrapped else "pre"}
            for side in ("top","right","bottom","left"):
                if getattr(xf.border,side+"_line_style"): style["border"+side.title()]="1px solid "+(xcolor(getattr(xf.border,side+"_colour_index")) or "#999999")
            cells.append({"row":r,"col":c,"text":display(value,book.format_map.get(xf.format_key).format_str if xf.format_key in book.format_map else "General"),"style":{k:v for k,v in style.items() if v is not None}})
        widths = {c:max(32,min(600,ws.colinfo_map[c].width/256*7)) if c in ws.colinfo_map else 96 for c in range(col,min(col+cols,ws.ncols))}
        heights = {r:max(26,min(300,ws.rowinfo_map[r].height/20*4/3)) if r in ws.rowinfo_map else 28 for r in range(row,min(row+rows,ws.nrows))}
    else:
        for r in range(row,min(row+rows,len(book))):
            for c in range(col,min(col+cols,len(book[r]))): cells.append({"row":r,"col":c,"text":book[r][c],"style":{}})
    return {"cells":cells,"merges":merges,"widths":widths,"heights":heights}
