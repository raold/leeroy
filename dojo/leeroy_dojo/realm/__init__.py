"""The dojo's disposable training realm: its own MySQL instance, schemas, configuration and worldserver.

Nothing here touches the live server. Every database call names the dojo instance explicitly, refuses a live port
before connecting and checks the server's identity before changing anything; credentials live only in git-ignored
files under the dojo runtime and reach client programs through --defaults-file, never a command line.
"""
