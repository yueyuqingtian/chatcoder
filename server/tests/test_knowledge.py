"""知识库服务逻辑测试（纯函数等价实现）。

与 task_service 测试风格一致：用纯函数实现等价逻辑，验证算法正确性。
DB 集成测试留待后续引入 testcontainers/PG 时补充。
"""


class FakeKB:
    def __init__(self, id: int, name: str, kb_type: str = "project"):
        self.id = id
        self.name = name
        self.type = kb_type


class FakeDoc:
    def __init__(self, id: int, kb_id: int, title: str, content: str, meta: dict | None = None):
        self.id = id
        self.kb_id = kb_id
        self.title = title
        self.content = content
        self.meta = meta


class KnowledgeStore:
    """模拟 knowledge_service 的内存实现，验证业务逻辑。"""

    def __init__(self):
        self._kbs: dict[int, FakeKB] = {}
        self._docs: dict[int, FakeDoc] = {}
        self._next_kb_id = 1
        self._next_doc_id = 1

    def create_kb(self, name: str, kb_type: str = "project") -> FakeKB:
        kb = FakeKB(id=self._next_kb_id, name=name, kb_type=kb_type)
        self._kbs[kb.id] = kb
        self._next_kb_id += 1
        return kb

    def get_kb(self, kb_id: int) -> FakeKB | None:
        return self._kbs.get(kb_id)

    def list_kbs(self) -> list[FakeKB]:
        return list(self._kbs.values())

    def delete_kb(self, kb_id: int) -> bool:
        if kb_id not in self._kbs:
            return False
        del self._kbs[kb_id]
        for doc_id in [d.id for d in self._docs.values() if d.kb_id == kb_id]:
            del self._docs[doc_id]
        return True

    def add_doc(self, kb_id: int, title: str, content: str, meta: dict | None = None) -> FakeDoc | None:
        if kb_id not in self._kbs:
            return None
        doc = FakeDoc(id=self._next_doc_id, kb_id=kb_id, title=title, content=content, meta=meta)
        self._docs[doc.id] = doc
        self._next_doc_id += 1
        return doc

    def get_doc(self, doc_id: int) -> FakeDoc | None:
        return self._docs.get(doc_id)

    def list_docs(self, kb_id: int) -> list[FakeDoc]:
        return [d for d in self._docs.values() if d.kb_id == kb_id]

    def delete_doc(self, doc_id: int) -> bool:
        if doc_id not in self._docs:
            return False
        del self._docs[doc_id]
        return True

    def search_docs(self, kb_id: int, keyword: str) -> list[FakeDoc]:
        kw = keyword.lower()
        return [
            d for d in self._docs.values()
            if d.kb_id == kb_id and (kw in d.title.lower() or kw in d.content.lower())
        ]


def test_create_and_get_kb():
    store = KnowledgeStore()
    kb = store.create_kb("测试知识库", "project")
    assert kb.id == 1
    assert kb.name == "测试知识库"
    assert kb.type == "project"

    fetched = store.get_kb(kb.id)
    assert fetched is not None
    assert fetched.id == kb.id


def test_get_nonexistent_kb_returns_none():
    store = KnowledgeStore()
    assert store.get_kb(999) is None


def test_list_kbs():
    store = KnowledgeStore()
    store.create_kb("KB1", "project")
    store.create_kb("KB2", "spec")

    kbs = store.list_kbs()
    assert len(kbs) == 2
    names = {kb.name for kb in kbs}
    assert "KB1" in names
    assert "KB2" in names


def test_delete_kb():
    store = KnowledgeStore()
    kb = store.create_kb("待删除")
    ok = store.delete_kb(kb.id)
    assert ok is True
    assert store.get_kb(kb.id) is None


def test_delete_nonexistent_kb_returns_false():
    store = KnowledgeStore()
    assert store.delete_kb(999) is False


def test_delete_kb_cascades_docs():
    store = KnowledgeStore()
    kb = store.create_kb("测试库")
    store.add_doc(kb.id, "文档1", "内容1")
    store.add_doc(kb.id, "文档2", "内容2")

    assert len(store.list_docs(kb.id)) == 2
    store.delete_kb(kb.id)
    assert len(store.list_docs(kb.id)) == 0


def test_add_and_list_docs():
    store = KnowledgeStore()
    kb = store.create_kb("文档测试库")

    doc1 = store.add_doc(kb.id, "文档1", "第一篇内容", {"tag": "guide"})
    doc2 = store.add_doc(kb.id, "文档2", "第二篇内容", {"tag": "api"})

    assert doc1 is not None
    assert doc2 is not None
    assert doc1.id == 1
    assert doc2.id == 2

    docs = store.list_docs(kb.id)
    assert len(docs) == 2
    titles = {d.title for d in docs}
    assert "文档1" in titles
    assert "文档2" in titles


def test_add_doc_to_nonexistent_kb_returns_none():
    store = KnowledgeStore()
    result = store.add_doc(999, "标题", "内容")
    assert result is None


def test_get_doc():
    store = KnowledgeStore()
    kb = store.create_kb("测试库")
    doc = store.add_doc(kb.id, "测试文档", "测试内容")

    fetched = store.get_doc(doc.id)
    assert fetched is not None
    assert fetched.title == "测试文档"
    assert fetched.content == "测试内容"


def test_delete_doc():
    store = KnowledgeStore()
    kb = store.create_kb("测试库")
    doc = store.add_doc(kb.id, "待删除", "xxx")

    ok = store.delete_doc(doc.id)
    assert ok is True
    assert store.get_doc(doc.id) is None


def test_delete_nonexistent_doc_returns_false():
    store = KnowledgeStore()
    assert store.delete_doc(999) is False


def test_search_docs_by_keyword():
    store = KnowledgeStore()
    kb = store.create_kb("搜索测试库")
    store.add_doc(kb.id, "Python 入门指南", "Python 是一门简洁的编程语言")
    store.add_doc(kb.id, "JavaScript 教程", "JavaScript 用于前端开发")
    store.add_doc(kb.id, "数据库设计", "PostgreSQL 与 MySQL 的对比")

    results = store.search_docs(kb.id, "Python")
    assert len(results) >= 1
    assert any("Python" in d.title for d in results)

    results = store.search_docs(kb.id, "前端")
    assert len(results) >= 1
    assert any("JavaScript" in d.title for d in results)

    results = store.search_docs(kb.id, "不存在的关键词")
    assert len(results) == 0


def test_search_is_case_insensitive():
    store = KnowledgeStore()
    kb = store.create_kb("测试库")
    store.add_doc(kb.id, "Hello World", "Python Programming")

    assert len(store.search_docs(kb.id, "python")) == 1
    assert len(store.search_docs(kb.id, "PYTHON")) == 1
    assert len(store.search_docs(kb.id, "hello")) == 1
