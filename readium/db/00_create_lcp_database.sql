-- Create the LCP database if it doesn't exist
SELECT 'CREATE DATABASE lcp'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'lcp')\gexec
