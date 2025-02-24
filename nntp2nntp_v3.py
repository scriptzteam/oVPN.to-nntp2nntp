#!/usr/bin/python -O
#
# nntp2nntp - base script "nntp2nntp_v3"
# mod by oVPN.to 
# + deny POSTing!
# + dynamic USER file
# Built: v3-0.1.6

import sys, os, time, requests, threading
from hashlib import sha256
from OpenSSL import SSL
from twisted.internet import ssl, reactor
from twisted.internet.protocol import ServerFactory, ClientFactory, Factory
from twisted.protocols.basic import LineReceiver
from twisted.python import log
try: from ConfigParser import SafeConfigParser
except: from configparser import SafeConfigParser

if len(sys.argv) != 2:
  sys.stderr.write("Usage: %s <config_file>\n" % sys.argv[0])
  sys.stderr.write("       %s pass\n" % sys.argv[0])
  sys.stderr.write("\nThe nntp2nntp is an NNTP proxy with SSL support and authentication mapping.\n\n")
  sys.stderr.write("<config_file>      Configuration file.\n")
  sys.stderr.write("pass               Ask for password and out string for configuration.\n")
  sys.stderr.write("\nExample of config file: (it is on stdout, you can simple redirect it)\n\n")
  sys.stdout.write("""[server]
use ssl = true
host = nntp.example.com
port = 563
login = myuser
password = mypwd
max connections = 50

[proxy]
use ssl = true
port = 1563
cert file = myserver.pem
cert key = myserver.key
ca verification = true
ca file = myca.pem
logfile = /var/log/nntp2nntp.log
pidfile = /var/run/nntp2nntp.pid
userfile = users.conf
""")
  sys.stdout.write("\nExample of user file:\n\n")
  sys.stdout.write("""[users]
user1    = 1b4f0e9851971998e732078544c96b36c3d01cedf7caa332359d6f1d83567014
user2    = 60303ae22b998861bce3b28f33eec1be758a213c86c93c076dbe9f558c11c752

[connections]
user1    = 10
user2    = 20

""")
  sys.exit(1)

if sys.argv[1].strip().upper() == 'PASS':
  import getpass
  pwd = getpass.getpass()
  print sha256(pwd).hexdigest()
  sys.exit(0)

config = SafeConfigParser()
config.read(sys.argv[1])

SERVER_HOST = config.get('server', 'host')
SERVER_PORT = config.has_option('server', 'port') and config.getint('server', 'port') or 119
SERVER_USER = config.get('server', 'login')
SERVER_PASS = config.get('server', 'password')
SERVER_SSL = config.has_option('server', 'use ssl') and config.getboolean('server', 'use ssl') or False
SERVER_CONNECTIONS = config.has_option('server', 'max connections') and config.getint('server', 'max connections') or 5
SERVER_PHONEPORT = config.has_option('server', 'phoneport') and config.getint('server', 'phoneport') or SERVER_PORT

PROXY_SSL = config.has_option('proxy', 'use ssl') and config.getboolean('proxy', 'use ssl') or False
PROXY_CERT_PEM = config.has_option('proxy', 'cert file') and config.get('proxy', 'cert file', '').strip() or ''
PROXY_CERT_KEY = config.has_option('proxy', 'cert key') and config.get('proxy', 'cert key').strip() or ''
PROXY_CA_VERIFY = config.has_option('proxy', 'ca verification') and config.getboolean('proxy', 'ca verification') or False
if PROXY_CA_VERIFY:
    PROXY_CERT_CA  = config.has_option('proxy', 'ca file') and config.get('proxy', 'ca file').strip() or ''
PROXY_PORT = config.has_option('proxy', 'port') and config.getint('proxy', 'port') or 1563
PROXY_LOGFILE = config.has_option('proxy', 'logfile') and config.get('proxy', 'logfile').strip() or '/var/log/nntp2nntp.log'
PROXY_PIDFILE = config.has_option('proxy', 'pidfile') and config.get('proxy', 'pidfile').strip() or '/var/run/nntp2nntp.pid'
USER_FILE = config.has_option('proxy', 'userfile') and config.get('proxy', 'userfile').strip()


def readusers(USER_FILE):
  usercfg = SafeConfigParser()
  usercfg.read(USER_FILE)
  USERS = dict(usercfg.items('users'))
  if usercfg.has_section('connections'):
    #log.msg("readusers")
    return USERS

def readconns(USER_FILE):
  usercfg = SafeConfigParser()
  usercfg.read(USER_FILE)
  if usercfg.has_section('connections'):
     #log.msg("readconns")
     USER_CONNECTIONS = dict([(x, int(y)) for x,y in usercfg.items('connections')])     
     return USER_CONNECTIONS

#LOCAL_USERS = readusers(USER_FILE)
#USER_CONNECTIONS = readconns(USER_FILE)


current_total_connections = 0
current_connections = {}

pid = os.fork()
if pid < 0: raise SystemError("Failed to start process")
elif pid > 0:
  fd = open(PROXY_PIDFILE, 'w')
  fd.write("%d" % pid)
  fd.close()
  sys.exit(0)

log.startLogging(file(PROXY_LOGFILE, 'a'))
Factory.noisy = False

class NNTPProxyServer(LineReceiver):
  clientFactory = None
  client = None
  auth_user = None

  def phoneHome(self):
    url = "https://vcp.ovpn.to/xxxnntp_.php"
    values = {'user' : self.auth_user, 'duration' : self.duration, 'downloaded' : self.downloaded_bytes, 'uploaded' : self.uploaded_bytes, 'port' : SERVER_PHONEPORT }
    try:
      r = requests.post(url,data=values)
      if r.content == "200":
        log.msg('user %s disconnected: duration %d, downloaded %d, uploaded %d (posted)' % (repr(self.auth_user),self.duration,self.downloaded_bytes,self.uploaded_bytes))
      else:
        raise Exception('POST FAILED')
    except:
      log.msg('user %s disconnected: duration %d, downloaded %d, uploaded %d' % (repr(self.auth_user),self.duration,self.downloaded_bytes,self.uploaded_bytes))

  def connectionMade(self):
    self.transport.pauseProducing()
    client = self.clientFactory()
    client.server = self
    if SERVER_SSL:
      reactor.connectSSL(SERVER_HOST, SERVER_PORT, client, ssl.ClientContextFactory())
    else:
      reactor.connectTCP(SERVER_HOST, SERVER_PORT, client)
    self.downloaded_bytes = 0
    self.uploaded_bytes = 0
    self.conn_time = time.time()

  def connectionLost(self, reason):
    global current_total_connections
    if self.client is not None:
	self.client.transport.loseConnection()
	self.client = None
    if current_connections.has_key(self.auth_user):
      current_connections[self.auth_user] = max(0, current_connections[self.auth_user] - 1)
    current_total_connections = max(0, current_total_connections - 1)
    self.duration = int(time.time() - self.conn_time)
    if self.duration >= 0 and self.downloaded_bytes > 22 and self.uploaded_bytes > 0:
      thread = threading.Thread(name='phonehome',target=self.phoneHome)
      thread.start()      

  def _lineReceivedNormal(self, line):
    self.uploaded_bytes += len(line)
    if line.upper().startswith('ARTICLE') or line.upper().startswith('BODY') or line.upper().startswith('HEAD') or line.upper().startswith('STAT'):
      self.client.sendLine(line)
      #log.msg("%s: %s" % (repr(self.auth_user),line))
      return
    elif line.upper().startswith('POST'):
      self.sendLine('440 Posting not permitted')
      log.msg("%s failed POST: %s" % (repr(self.auth_user),line))
      self.transport.loseConnection()
      return      
    elif line.upper().startswith('IHAVE'):
      self.sendLine('437 Article rejected; do not retry')
      log.msg("%s failed IHAVE: %s" % (repr(self.auth_user),line))
      self.transport.loseConnection()
      return
    elif line.upper().startswith('MODE READER'):
      self.sendLine('502 Reading service permanently unavailable')
      log.msg("%s failed MODE READER" % (repr(self.auth_user)))
      self.transport.loseConnection()
      return
    elif line.upper().startswith('QUIT'):
      self.sendLine('205 Connection closing')
      self.transport.loseConnection()
      return
    else:
      self.sendLine('502 Unknown command')
      log.msg("%s failed command: %s" % (repr(self.auth_user),line))
      self.transport.loseConnection()
      return
     

  def lineReceived(self, line):
    global LOCAL_USERS
    global USER_CONNECTIONS
    global current_total_connections
    if line.upper().startswith('AUTHINFO USER '):
      LOCAL_USERS = readusers(USER_FILE)
      USER_CONNECTIONS = readconns(USER_FILE)
      data = line.split(' ')
      if len(data) == 3: self.auth_user = data[2].strip()
      else: self.auth_user = ''
      if LOCAL_USERS.has_key(self.auth_user):
        self.client.sendLine('AUTHINFO USER %s' % SERVER_USER)
      else:
        self.sendLine('482 Invalid Username')
        #log.msg('user %s 482 Invalid Username' % (repr(self.auth_user)))
        self.transport.loseConnection()
        return
    elif line.upper().startswith('AUTHINFO PASS '):
      data = line.split(' ')
      if len(data) == 3 and LOCAL_USERS.get(self.auth_user) == sha256(data[2].strip()).hexdigest():
        if not current_connections.has_key(self.auth_user): current_connections[self.auth_user] = 1
        else: current_connections[self.auth_user] = current_connections[self.auth_user] + 1
        current_total_connections = current_total_connections + 1
        if USER_CONNECTIONS.has_key(self.auth_user):
          if current_connections[self.auth_user] > USER_CONNECTIONS[self.auth_user] or current_total_connections > SERVER_CONNECTIONS:
            self.sendLine('502 Too many connections')
            #log.msg('user %s 502 Too many connections' % (repr(self.auth_user)))
            self.transport.loseConnection()
            return
        self.client.sendLine('AUTHINFO PASS %s' % SERVER_PASS)
        log.msg("%s successfully logged in (%d. conn [%s/%s])" % (repr(self.auth_user), current_connections[self.auth_user],current_total_connections,SERVER_CONNECTIONS))
	#log.msg("%s line= %s" % (repr(self.auth_user),line))
	self.lineReceived = self._lineReceivedNormal
      else:
        self.sendLine('482 Invalid Password')
        self.transport.loseConnection()
        return
      #self.lineReceived = self._lineReceivedNormal
    elif line.upper().startswith('MODE READER'):
		self.sendLine('502 Reading service permanently unavailable')
		log.msg("%s failed MODE READER" % (repr(self.auth_user)))
		self.transport.loseConnection()
		return
#   elif line.upper().startswith('HEAD'):
#		self.sendLine('430 no article with that message-id')
#		log.msg("%s failed HEAD" % (repr(self.auth_user)))
#		self.transport.loseConnection()
#    elif line.upper().startswith('POST'):
#		self.sendLine('440 Posting not permitted')
#		log.msg("%s failed POST" % (repr(self.auth_user)))
#		self.transport.loseConnection()
#                return
#    elif line.upper().startswith('IHAVE'):
#		self.sendLine('435 Article not wanted')
#		log.msg("%s failed IHAVE" % (repr(self.auth_user)))
#		self.transport.loseConnection()
#                return
#    elif line.upper().startswith('BODY'):
#		self.sendLine('430 no article with that message-id')
#		log.msg("%s failed BODY" % (repr(self.auth_user)))
#		self.transport.loseConnection()
    elif line.upper().startswith('QUIT'):
		self.sendLine('205 Connection closing')
		log.msg("%s sent QUIT" % (repr(self.auth_user)))
		self.transport.loseConnection()
		return
#    elif line == '':
#		self.sendLine('502 ERROR YOU SENT NOTHING')
#		log.msg("%s sent NULL" % (repr(self.auth_user)))
#		self.transport.loseConnection()
#                return
    else:
	# debug
		log.msg("%s failed else: line= %s" % (repr(self.auth_user),line))
		self.sendLine('502 Should never see this')
		self.transport.loseConnection()
		return
		#self._lineReceivedNormal(line)

class NNTPProxyClient(LineReceiver):
  server = None

  def connectionMade(self):
    self.server.client = self
    self.server.transport.resumeProducing()

  def connectionLost(self, reason):
    if self.server is not None:
	self.server.transport.loseConnection()
	self.server = None

  def lineReceived(self, line):
    self.server.downloaded_bytes += len(line)
    if line.startswith('200 ') or line.startswith('201 '):
       line = "201 oVPN NNTP Service available, posting prohibited"
       self.server.sendLine(line)
    elif line.startswith('502 Authentication'):
       log.msg('DL: %s' % (line))       
       line = "502 NNTP Gateway Port unavailable."
       self.server.sendLine(line)
       self.server.transport.loseConnection()
       self.transport.loseConnection()
    else:
       self.server.sendLine(line)



class NNTPProxyClientFactory(ClientFactory):
  server = None
  protocol = NNTPProxyClient

  def buildProtocol(self, *args, **kw):
    prot = ClientFactory.buildProtocol(self, *args, **kw)
    prot.server = self.server
    return prot

  def clientConnectionLost(self, connector, reason):
    self.server.transport.loseConnection()

  def clientConnectionFailed(self, connector, reason):
    self.server.transport.loseConnection()

def verifyCallback(connection, x509, errnum, errdepth, ok):
  if not ok:
    log.msg('invalid cert from subject: %s' % x509.get_subject())
    return False
  log.msg('accepted cert from subject: %s' % x509.get_subject())
  return True

serverFactory = ServerFactory()
serverFactory.protocol = NNTPProxyServer
serverFactory.protocol.clientFactory = NNTPProxyClientFactory
if PROXY_SSL:
  sslFactory = ssl.DefaultOpenSSLContextFactory(PROXY_CERT_KEY, PROXY_CERT_PEM)
  sslContext = sslFactory.getContext()
  if PROXY_CA_VERIFY:
      sslContext.set_verify(SSL.VERIFY_PEER | SSL.VERIFY_FAIL_IF_NO_PEER_CERT, verifyCallback)
      sslContext.set_verify_depth(10)
      sslContext.load_verify_locations(PROXY_CERT_CA)
  reactor.listenSSL(PROXY_PORT, serverFactory, sslFactory)
else:
  reactor.listenTCP(PROXY_PORT, serverFactory)
reactor.run()

# vim:sts=2:sw=2:
