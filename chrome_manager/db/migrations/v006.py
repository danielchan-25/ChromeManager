"""Reserved schema revision; external notification storage was removed."""
VERSION = 6

def apply(connection):
    connection.execute("UPDATE schema_version SET version=6")
