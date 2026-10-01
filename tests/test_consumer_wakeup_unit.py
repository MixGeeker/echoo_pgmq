"""Compile the actual queue invalidator with minimal ordinary scheduler stubs."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'src/echoo_pgmq.c'


def function(source, name, return_type="void"):
    start = source.index('static ' + return_type + '\n' + name + '(')
    body = source.index('{', start)
    depth = 1
    end = body + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


class WakeupTests(unittest.TestCase):
    def test_post_commit_call_and_unchanged_claim_guards(self):
        source = SOURCE.read_text()
        publish = function(source, 'receive_message')
        success = publish.split('else if (echoo_db_publish', 1)[1].split('\n    else\n', 1)[0]
        self.assertEqual(source.count('invalidate_empty_consumers(link->queue);'), 1)
        self.assertLess(success.index('invalidate_empty_consumers'), success.index('PN_ACCEPTED'))
        pump = function(source, 'pump_sender')
        for guard in ('now < link->next_poll', 'pn_link_credit(link->link) <= 0',
                      'link->inflight >= max_inflight', 'outgoing_bytes(connection)',
                      'total_buffered_bytes()', 'now >= receipt->deadline'):
            self.assertIn(guard, pump[:pump.index('result = echoo_db_claim')])
        self.assertIn('static int poll_interval_ms = 50;', source)
        self.assertIn('link->next_poll = now + poll_interval_ms;', pump)
        storage = (ROOT / 'src/storage.c').read_text()
        publish_db = storage.split('echoo_db_publish(', 1)[1].split('PG_END_TRY();', 1)[0]
        self.assertLess(publish_db.index('commit_operation();'), publish_db.index('success = true;'))

    def test_actual_c_invalidation(self):
        code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
typedef struct Link { bool sender; } Link;
typedef struct EchooLink {
 Link *link; char *queue; int64_t next_poll; bool closed; struct EchooLink *next;
} EchooLink;
typedef struct EchooConnection {
 bool failed; int64_t closing_at; EchooLink *links; struct EchooConnection *next;
} EchooConnection;
static EchooConnection *connections;
static int MyLatch, wakes;
static bool pn_link_is_sender(Link *l) { return l->sender; }
static void SetLatch(int latch) { (void) latch; ++wakes; }
''' + function(SOURCE.read_text(), 'invalidate_empty_consumers') + r'''
int main(void) {
 Link sending = {true}, receiving = {false};
 EchooLink links[7] = {
  {&sending, "q", 500, false, 0},
  {&sending, "other", 500, false, 0},
  {&receiving, "q", 500, false, 0},
  {&sending, "q", 500, true, 0},
  {&sending, "q", 0, false, 0},
  {&sending, "q", 500, false, 0},
  {&sending, "q", 500, false, 0}
 };
 EchooConnection c[3] = {{false,0,&links[0],0}, {true,0,&links[5],0}, {false,1,&links[6],0}};
 int i;
 for (i=0;i<4;i++) links[i].next=&links[i+1];
 c[0].next=&c[1]; c[1].next=&c[2]; connections=&c[0];
 invalidate_empty_consumers("absent"); assert(wakes==0);
 invalidate_empty_consumers("q"); assert(wakes==1 && links[0].next_poll==0);
 for (i=1;i<4;i++) assert(links[i].next_poll==500);
 assert(links[4].next_poll==0 && links[5].next_poll==500 && links[6].next_poll==500);
 invalidate_empty_consumers("q"); assert(wakes==1);
 /* All matching consumers are invalidated, irrespective of connection order. */
 c[1].failed=false; c[2].closing_at=0;
 invalidate_empty_consumers("q"); assert(wakes==2 && links[5].next_poll==0 && links[6].next_poll==0);
 links[0].next_poll=800; invalidate_empty_consumers("q"); assert(wakes==3);
 connections=0; invalidate_empty_consumers("q"); assert(wakes==3);
 return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'wake.c').write_text(code)
            compiler = shutil.which('cc') or shutil.which('gcc')
            if compiler:
                exe = root / 'wake'
                subprocess.run([compiler, '-std=c99', '-Wall', '-Wextra', '-Werror', str(root/'wake.c'), '-o', str(exe)], check=True)
            else:
                (root / 'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.16)\nproject(wake C)\nadd_executable(wake wake.c)\n')
                subprocess.run(['cmake', '-S', str(root), '-B', str(root/'build')], check=True)
                subprocess.run(['cmake', '--build', str(root/'build'), '--config', 'Debug'], check=True)
                matches = list((root/'build').rglob('wake.exe'))
                self.assertEqual(len(matches), 1)
                exe = matches[0]
            subprocess.run([str(exe)], check=True)


if __name__ == '__main__':
    unittest.main()
