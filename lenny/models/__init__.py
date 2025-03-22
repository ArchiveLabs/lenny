#!/usr/bin/env python

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.ext.declarative import declarative_base

from lenny.configs import DB_URI, DEBUG

engine = create_engine(DB_URI, echo=DEBUG, client_encoding='utf8')
db = scoped_session(sessionmaker(bind=engine))
