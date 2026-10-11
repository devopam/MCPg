SELECT pg_read_file('/etc/passwd'), lo_import('/etc/passwd'), pg_terminate_backend(1)
