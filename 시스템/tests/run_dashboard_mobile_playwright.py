#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""모바일 대시보드 Playwright 회귀 테스트 실행기."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["CNV_DASHBOARD_PLAYWRIGHT"] = "1"

suite = unittest.defaultTestLoader.loadTestsFromName("시스템.tests.test_dashboard_mobile_playwright")
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
