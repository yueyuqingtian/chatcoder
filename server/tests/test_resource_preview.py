"""plan-20261103-attachment-preview：资源权限、目录、格式与区域读取回归。"""
import io
from pathlib import Path
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from app.gateway.routers import upload
from app.persistence.database import async_session_factory, run_write_locked
from app.persistence.models.message import Session, Message
from app.persistence.models.project import Project
from app.services.preview_service import decode_text, safe_project_path
from app.services.spreadsheet_preview import metadata, region
from fastapi import HTTPException


@pytest.fixture
async def preview(tmp_path, monkeypatch):
    root = tmp_path / "项目"; root.mkdir()
    uploads = tmp_path / "uploads"; uploads.mkdir()
    monkeypatch.setattr(upload, "uploads_root", lambda: uploads)
    def seed(db):
        project = Project(name="项目",path=str(root)); db.add(project); db.flush()
        a=Session(project_id=project.id,title="甲"); b=Session(title="乙")
        db.add_all([a,b]);db.flush()
        m=Message(session_id=a.id,sender_type="user",msg_type="text",content={"attachments":[
            {"file_id":"a","filename":"说明.md","path":"a/说明.md"},
            {"file_id":"b","filename":"same.py","path":"b/same.py"}]})
        n=Message(session_id=b.id,sender_type="user",msg_type="text",content={"attachments":[{"file_id":"a","filename":"说明.md","path":"a/说明.md"}]})
        db.add_all([m,n]);db.flush(); ids=(a.id,b.id,project.id,m.id,n.id);db.commit();return ids
    ids=await run_write_locked(seed)
    for rel,text in (("a/说明.md","# 外部副本"),("b/same.py","print('你好')")):
        p=uploads/rel;p.parent.mkdir();p.write_text(text,encoding="utf-8")
    app=FastAPI();app.include_router(upload.router,prefix="/api")
    async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as client:
        yield client,root,uploads,ids


@pytest.mark.asyncio
async def test_attachment_membership_history_and_no_project(preview):
    client,root,uploads,(a,b,p,m,n)=preview
    query={"source":"attachment","session_id":a,"message_id":m,"file_id":"a"}
    response=await client.get("/api/preview/resource",params=query)
    assert response.status_code==200 and response.json()["content"]=="# 外部副本"
    assert (await client.get("/api/preview/resource",params={**query,"session_id":b})).status_code==404
    assert (await client.get("/api/preview/resource",params={**query,"file_id":"not-owned"})).status_code==404
    assert (await client.get("/api/preview/resource",params={**query,"session_id":b,"message_id":n})).status_code==200
    assert (await client.get("/api/preview/resource",params={**query,"relative":"same.py"})).json()["content"]=="print('你好')"
    assert (await client.get("/api/preview/resource",params={**query,"relative":"../secret"})).status_code==403
    (uploads/"a/说明.md").unlink()
    assert (await client.get("/api/preview/resource",params=query)).status_code==404


@pytest.mark.asyncio
async def test_directory_absolute_relative_deep_and_worktree(preview,tmp_path):
    client,root,uploads,(a,b,p,m,n)=preview
    deep=root.joinpath(*[f"目录 {i}" for i in range(11)]);deep.mkdir(parents=True)
    file=deep/"code.ts";file.write_text("const x=1",encoding="utf-8")
    query={"source":"project","session_id":a,"project_id":p}
    for path in ("",str(root),str(deep),str(deep.relative_to(root)).replace("/","\\")):
        r=await client.get("/api/preview/resource",params={**query,"path":path})
        assert r.status_code==200 and r.json()["kind"]=="directory"
    assert (await client.get("/api/preview/children",params={**query,"path":str(deep)})).json()["children"][0]["name"]=="code.ts"
    assert (await client.get("/api/preview/resource",params={**query,"path":"../outside"})).status_code==403
    assert (await client.get("/api/preview/resource",params={**query,"session_id":b,"path":""})).status_code==403
    worktree=tmp_path/"worktree";worktree.mkdir();(worktree/"other.py").write_text("# 工作树",encoding="utf-8")
    def change(db):
        s=db.get(Session,a);s.worktree_path=str(worktree);db.commit()
    await run_write_locked(change)
    assert (await client.get("/api/preview/resource",params={**query,"path":"other.py"})).json()["content"]=="# 工作树"
    assert (await client.get("/api/preview/resource",params={**query,"path":str(file)})).status_code==403


@pytest.mark.asyncio
async def test_upload_filename_and_mime(preview):
    client,*_=preview
    for name in ("../a.txt","C:bad.txt","a\\b.txt"):
        r=await client.post("/api/upload",files={"file":(name,b"hello","text/plain")})
        assert r.status_code==400
    r=await client.post("/api/upload",files={"file":("table.xlsx",b"bad","application/octet-stream")})
    assert r.status_code==200
    assert r.json()["mime_type"]=="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.mark.asyncio
async def test_text_encodings_truncation_and_binary(preview):
    client,root,_,(a,b,p,m,n)=preview
    q={"source":"project","session_id":a,"project_id":p,"path":"text.txt"}
    for encoding in ("utf-8-sig","utf-16","gb18030"):
        (root/"text.txt").write_bytes("中文内容".encode(encoding))
        r=await client.get("/api/preview/resource",params=q)
        assert r.json()["content"]=="中文内容"
    (root/"text.txt").write_bytes(b"\x00binary")
    assert (await client.get("/api/preview/resource",params=q)).status_code==415
    (root/"text.txt").write_bytes(b"a"*(512*1024+1))
    r=await client.get("/api/preview/resource",params=q)
    assert r.json()["truncated"] and len(r.json()["content"])==512*1024


def test_xlsx_styles_merge_formula_and_later_region(tmp_path):
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    wb=openpyxl.Workbook();ws=wb.active;ws.title="数据"
    ws["A1"]="合并标题";ws.merge_cells("A1:C2");ws["A1"].font=Font(bold=True,color="FF112233")
    ws["A1"].fill=PatternFill("solid",fgColor="FFFFEEAA")
    ws["B202"]=0.125;ws["B202"].number_format="0.0%"
    ws["C203"]="=1+2";wb.create_sheet("第二张")
    p=tmp_path/"book.xlsx";wb.save(p)
    assert len(metadata(p)["sheets"])==2
    block=region(p,"数据",1,1)
    assert [0,0,2,3] in block["merges"]
    anchor=next(c for c in block["cells"] if c["row"]==0 and c["col"]==0)
    assert anchor["style"]["fontWeight"]=="bold" and anchor["style"]["backgroundColor"]=="#FFEEAA"
    block=region(p,"数据",200,0)
    assert next(c for c in block["cells"] if c["row"]==201 and c["col"]==1)["text"]=="12.5%"
    assert "无缓存结果" in next(c for c in block["cells"] if c["row"]==202 and c["col"]==2)["text"]
    p=tmp_path/"broken.xlsx";p.write_bytes(b"bad")
    with pytest.raises(HTTPException) as e: metadata(p)
    assert e.value.status_code==422


def test_xls_and_csv(tmp_path):
    fixture=Path(__file__).parent/"fixtures/preview-styles.xls"
    assert metadata(fixture)["sheets"][0]["rows"]>200
    block=region(fixture,"样式",200,0)
    assert any(c["text"]=="第 202 行" for c in block["cells"])
    block=region(fixture,"样式",0,0)
    assert block["merges"]==[[0,0,2,3]]
    assert block["cells"][0]["style"]["fontWeight"]=="bold"
    p=tmp_path/"table.csv";p.write_text('姓名,备注\n张三,"第一行\n第二行"',encoding="utf-8")
    assert metadata(p)["sheets"][0]["rows"]==2
    assert "第一行" in region(p,"table",0,0)["cells"][-1]["text"]


@pytest.mark.asyncio
async def test_content_headers_and_corrupt_download(preview):
    client,root,_,(a,b,p,m,n)=preview
    q={"source":"project","session_id":a,"project_id":p,"path":"broken.docx"}
    (root/"broken.docx").write_bytes(b"broken")
    assert (await client.get("/api/preview/content",params=q)).status_code==422
    r=await client.get("/api/preview/content",params={**q,"download":True})
    assert r.status_code==200 and "attachment" in r.headers["content-disposition"]
    (root/"active.html").write_text("<script>alert(1)</script>")
    r=await client.get("/api/preview/content",params={**q,"path":"active.html"})
    assert "attachment" in r.headers["content-disposition"] and "sandbox" in r.headers["content-security-policy"]
    (root/"unicode.txt").write_bytes(("中"*200000).encode("utf-8"))
    r=await client.get("/api/preview/resource",params={**q,"path":"unicode.txt"})
    assert r.status_code==200 and r.json()["content"]=="中"*(512*1024//3)


@pytest.mark.asyncio
async def test_unsent_upload_preview_without_session(preview):
    client,_,uploads,_=preview
    r=await client.post("/api/upload",files={"file":("草稿.md","# 尚未发送".encode("utf-8"),"text/markdown")})
    att=r.json()
    q={"source":"upload","file_id":att["file_id"],"filename":att["filename"]}
    r=await client.get("/api/preview/resource",params=q)
    assert r.status_code==200 and r.json()["content"]=="# 尚未发送"
    assert (await client.get("/api/preview/resource",params={**q,"filename":"../secret"})).status_code==400
    assert (await client.get("/api/preview/resource",params={**q,"file_id":"../outside"})).status_code==400
    r=await client.get("/api/preview/content",params={**q,"download":True})
    assert r.status_code==200 and "attachment" in r.headers["content-disposition"]
