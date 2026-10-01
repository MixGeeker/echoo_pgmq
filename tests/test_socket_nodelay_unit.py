"""Real loopback IPv4/IPv6 exercise of the actual accepted-socket helper."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_consumer_wakeup_unit import SOURCE, function


class SocketTests(unittest.TestCase):
    def test_real_loopback_and_error_paths(self):
        code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
typedef SOCKET pgsocket;
typedef int socklen_t;
#define close_socket closesocket
#define BAD_SOCKET INVALID_SOCKET
#else
#include <unistd.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
typedef int pgsocket;
#define close_socket close
#define BAD_SOCKET -1
#endif
static int logs, force_nonblock_failure, pretend_nonblock_success;
#define ereport(level, args) do { ++logs; } while (0)
static bool pg_set_noblock(pgsocket fd) {
 if (force_nonblock_failure) return false;
 if (pretend_nonblock_success) return true;
#ifdef _WIN32
 u_long one=1; return ioctlsocket(fd,FIONBIO,&one)==0;
#else
 int flags=fcntl(fd,F_GETFL,0); return flags>=0 && fcntl(fd,F_SETFL,flags|O_NONBLOCK)==0;
#endif
}
''' + function(SOURCE.read_text(),'configure_client_socket','bool') + r'''
static void check_family(int family) {
 pgsocket listener=socket(family,SOCK_STREAM,IPPROTO_TCP), client, accepted;
 struct sockaddr_storage address; socklen_t length; int value=0; socklen_t optlen=sizeof(value);
 assert(listener!=BAD_SOCKET); memset(&address,0,sizeof(address));
 if (family==AF_INET) {
  struct sockaddr_in *a=(struct sockaddr_in *)&address;
  a->sin_family=AF_INET;a->sin_addr.s_addr=htonl(INADDR_LOOPBACK);length=sizeof(*a);
 } else {
  struct sockaddr_in6 *a=(struct sockaddr_in6 *)&address;
  a->sin6_family=AF_INET6;a->sin6_addr=in6addr_loopback;length=sizeof(*a);
 }
 assert(bind(listener,(struct sockaddr *)&address,length)==0);
 assert(listen(listener,1)==0);
 assert(getsockname(listener,(struct sockaddr *)&address,&length)==0);
 client=socket(family,SOCK_STREAM,IPPROTO_TCP);assert(client!=BAD_SOCKET);
 assert(connect(client,(struct sockaddr *)&address,length)==0);
 accepted=accept(listener,0,0);assert(accepted!=BAD_SOCKET);
 assert(configure_client_socket(accepted));
 assert(getsockopt(accepted,IPPROTO_TCP,TCP_NODELAY,(char *)&value,&optlen)==0 && value==1);
#ifndef _WIN32
 assert(fcntl(accepted,F_GETFL,0)&O_NONBLOCK);
#endif
 close_socket(accepted);close_socket(client);close_socket(listener);
}
int main(void) {
#ifdef _WIN32
 WSADATA data;assert(WSAStartup(MAKEWORD(2,2),&data)==0);
#endif
 check_family(AF_INET);check_family(AF_INET6);assert(logs==0);
 force_nonblock_failure=1;assert(!configure_client_socket(BAD_SOCKET));assert(logs==0);
 force_nonblock_failure=0;pretend_nonblock_success=1;
 assert(!configure_client_socket(BAD_SOCKET));assert(logs==1);
#ifdef _WIN32
 WSACleanup();
#endif
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'socket_test.c').write_text(code)
            compiler=shutil.which('cc') or shutil.which('gcc')
            if compiler:
                exe=root/'socket_test'
                flags=['-lws2_32'] if __import__('os').name=='nt' else []
                subprocess.run([compiler,'-std=c99','-Wall','-Wextra','-Werror',str(root/'socket_test.c'),'-o',str(exe),*flags],check=True)
            else:
                (root/'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.16)\nproject(socket_test C)\nadd_executable(socket_test socket_test.c)\nif(WIN32)\ntarget_link_libraries(socket_test ws2_32)\nendif()\n')
                subprocess.run(['cmake','-S',str(root),'-B',str(root/'build')],check=True)
                subprocess.run(['cmake','--build',str(root/'build'),'--config','Debug'],check=True)
                matches=list((root/'build').rglob('socket_test.exe'));self.assertEqual(len(matches),1);exe=matches[0]
            subprocess.run([str(exe)],check=True,timeout=15)

    def test_failed_configuration_closes_before_protocol_setup(self):
        accept=function(SOURCE.read_text(),'accept_connections')
        gate=accept[accept.index('!configure_client_socket'):accept.index('connection = calloc')]
        self.assertIn('closesocket(socket_fd);',gate)
        self.assertIn('continue;',gate)
        self.assertNotIn('setsockopt',function(SOURCE.read_text(),'receive_message'))


if __name__=='__main__':unittest.main()
