"""网关层：REST/WS 入口、鉴权、路由组织。

MVP 阶段为 Python 实现；演进时此层可独立为 Java Spring Boot 服务，
通过 gRPC 调用编排层。模块边界已按拆分点划分。
"""
