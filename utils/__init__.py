# -*- coding: utf-8 -*-
"""utils 包 — 纯工具函数(零 SDK 依赖)。

⚠ 铁律与 env_tools/__init__.py 相同: 任何在 bootstrap_env() 之前被 import 的
模块都不得触达 SDK。本包只放标准库依赖的纯工具; 一旦某个模块要 import SDK,
只能让它出现在 bootstrap_env() 之后(入口文件保证顺序)。
"""
