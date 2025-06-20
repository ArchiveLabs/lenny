#!/usr/bin/env python

"""
    Core module for Lenny, s3 & db
    
    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import boto3
from lenny.core.s3 import LennyS3
from lenny.core import db as database
from lenny.core import models

db = database.init()
s3 = LennyS3()

__all__ = ["s3", "Base", "db", "engine", "items", "init_db"]
