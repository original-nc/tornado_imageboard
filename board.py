import tornado.options
import tornado.httpserver
import tornado.web
import tornado.ioloop
import motor.motor_tornado
import uimodules
import datetime
import ib_settings as _ib
from tornado import concurrent
import re
from uuid import uuid4
import os
from mimetypes import guess_type
import json
from getresolution import resolution
from tornado import gen
from html.parser import HTMLParser
from PIL import Image
import geoip2.database as gdb
from thumbnail import make_thumbnail
from tripcode import tripcode

from tornado.options import define, options
define('port', default=8000, help='run on given port', type=int)

executor = concurrent.futures.ThreadPoolExecutor(8)

uploads = 'uploads/'

thumb_def = 'static/missing_thumbnail.jpg'
spoilered = 'static/spoiler.jpg'

def check_uploads():
    if not os.path.exists(uploads):
        os.makedirs(uploads)

gdbr = gdb.Reader('GeoLite2-Country.mmdb')

class MLStripper(HTMLParser):
    def __init__(self):
        self.reset()
        self.strict = False
        self.convert_charrefs= True
        self.fed = []
    def handle_data(self, d):
        self.fed.append(d)
    def get_data(self):
        return ''.join(self.fed)


# this is done to ensure user does not input any html in posting form
def strip_tags(html):
    s = MLStripper()
    s.feed(html)
    return s.get_data()


# decorator that checks if user is admin
def ifadmin(f):
    def wrapper(self, *args, **kwargs):
        if not self.current_user:
            self.redirect('/admin/login')
        return f(self, *args, *kwargs)
    return wrapper


# crappy handler that checks if user is admin
class LoggedInHandler(tornado.web.RequestHandler):
    def get_current_user(self):
        return self.get_secure_cookie('adminlogin')


# list of boards
class IndexHandler(tornado.web.RequestHandler):

    async def get(self):
        db = self.application.database
        boards = await db.boards.find({}).to_list(None) or None
        boards_list = await db.boards.find({}).to_list(None) or None
        self.render('index.html', boards=boards, boards_list=boards_list)


# list of threads, more like catalog
class BoardHandler(LoggedInHandler):

    async def get(self, board):
        db = self.application.database
        db_board = await db.boards.find_one({'short': board})
        if db_board:
            threads = await db['posts'].find({'board': board,'oppost': True}).sort([('lastpost', -1)]).limit(db_board['thread_catalog']).to_list(None)
            boards_list = await db.boards.find({}).to_list(None)
            admin = False
            if self.current_user:
                admin = True
            self.render('board.html', threads=threads, board=db_board, boards_list=boards_list, admin=admin)
        else:
            self.redirect('/')

    async def post(self, board):
        db = self.application.database
        ip = await get_ip(self.request)
        if not await is_banned(db, ip):
            db_board = await db.boards.find_one({'short': board})
            threads = await db['posts'].find({'board': board,'oppost': True}).sort([('lastpost', -1)]).limit(db_board['thread_catalog']).to_list(None)
            subject = self.get_argument('subject', '')
            text = self.get_argument('text', '')
            username = self.get_argument('username', '') or False
            text = strip_tags(text)
            text = text.replace("\n","<br />")
            spoiler = 'spoilerimage' in self.request.arguments
            if self.request.files:
                fo, ff, filetype, filedata = await upload_file(self.request.files['file'][0])
            else:
                fo = ff = filetype = filedata = None
            count = await latest(db) + 1
            oppost = True
            thread = None
            data = await makedata(db, subject, text, count, board, ip, oppost, thread, fo, ff, filetype, filedata, username, spoiler=spoiler)
            await db.posts.insert(data)
            self.redirect('/' + board + '/thread/' + str(data['count']))
        else:
            self.redirect('/banned')


# posts in thread
class ThreadHandler(LoggedInHandler):
    thread_count = ''

    async def get(self, board, count):
        thread_count = int(count)
        db = self.application.database
        db_board = await db.boards.find_one({'short': board})
        posts = await db['posts'].find({'thread': thread_count}).sort([('count', 1)]).to_list(None)
        op = await db['posts'].find_one({"count": thread_count})
        if op:
            if await check_thread(db, thread_count, db_board['thread_posts']):
                op['locked'] = True
                await update_db(db, op['count'], op)
            boards_list = await db.boards.find({}).to_list(None)
            admin = False
            if self.current_user:
                admin = True
            self.render('posts.html', op=op, posts=posts, board=db_board, boards_list=boards_list, admin=admin)

        else:
            self.redirect('/' + board)

    async def post(self, board, thread_count):
        db = self.application.database
        ip = await get_ip(self.request)
        if not await is_banned(db, ip):
            thread_count = int(thread_count)
            subject = self.get_argument('subject', '')
            text = self.get_argument('text', 'empty post')
            text = strip_tags(text)
            text = text.replace("\n","<br />")
            username = self.get_argument('username', '') or False
            if self.request.files:
                foriginal, ffile, filetype, filedata = await upload_file(self.request.files['file'][0])
            else:
                foriginal = ffile = filetype = filedata = None
            replies = get_replies(text)
            count = await latest(db) + 1
            oppost = False
            thread = thread_count
            ip = await get_ip(self.request)
            spoiler = 'spoilerimage' in self.request.arguments
            data = await makedata(db, subject, text, count, board, ip, oppost, thread, foriginal, ffile, filetype, filedata,
                username, spoiler=spoiler)
            await db.posts.insert(data)
            op = await db['posts'].find_one({'count': thread_count})
            if op:
                db_board = await db.boards.find_one({'short': board})
                if not op['locked']:
                    if not await check_thread(db, thread_count, db_board['thread_bump']):
                        if not data['subject'] == 'sage':
                            op['lastpost'] = datetime.datetime.utcnow()
                            await update_db(db, op['count'], op)
                    for number in replies:
                        p = await db.posts.find_one({'count': int(number)})
                        old_replies = p['replies']
                        if int(data['count']) not in old_replies:
                            old_replies.append(int(data['count']))
                            p['replies'] = old_replies
                            await update_db(db, p['count'], p)
                if op != None:
                    if await check_thread(db, thread_count, db_board['thread_posts']):
                        op['locked'] = True
                        await update_db(db, op['count'], op)
                self.redirect('/' + str(board) + '/thread/' + str(op['count']))
            else:
                self.redirect('/' + str(board))
        else:
            self.redirect('/banned')


class JsonThreadHandler(LoggedInHandler):
    thread_count = ''

    async def get(self, board, count):
        thread_count = int(count)
        db = self.application.database
        db_board = await db.boards.find_one({'short': board})
        op = await db.posts.find_one({'count': thread_count})
        res = [op]
        del op['_id']
        op['date'] = op['date'].strftime("%Y-%m-%d %H:%M:%S")
        op['lastpost'] = op['lastpost'].strftime("%Y-%m-%d %H:%M:%S")
        posts = await db['posts'].find({'thread': thread_count}).sort([('count', 1)]).to_list(None)
        for post in posts:
            del post['_id']
            post['date'] = post['date'].strftime("%Y-%m-%d %H:%M:%S")
            res.append(post)
        self.write(json.dumps(res, indent=4))


async def upload_file(f):
    fname = f['filename']
    fext = os.path.splitext(fname)[1]
    if fext in ['.jpg', '.gif', '.png','.jpeg']:
        filetype = 'image'
    elif fext in ['.webm', '.mp4']:
        filetype = 'video'
    else:
        # if format not supported
        return None, None, None, None
    newname = uploads + str(uuid4()) + fext
    with open(newname, 'wb') as nf:
        nf.write(bytes(f['body']))
    filedata = await process_file(newname)
    return fname, newname, filetype, filedata


async def convert_bytes(num):
    for x in ['bytes', 'KB', 'MB', 'GB', 'TB']:
        if num < 1024.0:
            return "%3.1f %s" % (num, x)
        num /= 1024.0


async def process_file(fn):

    if os.path.isfile(fn):
        filesize = await convert_bytes((os.stat(fn).st_size))
        if fn.endswith(('webm', 'mp4')):
            w,h = resolution(fn)
        else:
            with Image.open(fn) as img:
                w, h = img.size
        return '{0}, {1}x{2}, {3}'.format(fn.split('.')[-1].upper(), w, h, filesize)
    else:
        return False


# delete posts using ajax; doesnt have admin rights check and idk how to make it
class AjaxDeleteHandler(tornado.web.RequestHandler):

    async def post(self):
        db = self.application.database
        data = dict((k,v[-1] ) for k, v in self.request.arguments.items())
        pid = int(data['post'].decode('utf-8'))
        post = await db.posts.find_one({'count': pid})
        if not post['oppost']:
            posts = await db.posts.find({'thread': pid}).to_list(None)
            await self.delete_post(post, pid)
            for post in posts:
                await self.delete_post(post, pid)
            response = {'succ':'ess'}
        else:
            await self.delete_post(post, pid)
            response = {'succ':'ess'}
            response['op'] = 'true'
        self.write(json.dumps(response))

    async def delete_post(self, post, pid):
        files = []
        db = self.application.database
        if post['image']:
            files.append(post['image'])
            if post['thumb'] != thumb_def and post['thumb'] != spoilered:
                files.append(post['thumb'])
        elif post['video']:
            files.append(post['video'])
            if post['thumb'] != thumb_def and post['thumb'] != spoilered:
                files.append(post['thumb'])
            await db.posts.delete_many({'thread': pid})
        await db.posts.delete_one({'count': pid})
        await self.delete(files)

    async def delete(self, files):
        for file in files:
            os.remove(file)


# reporting users using ajax; same stuff as with previous one
class AjaxReportHandler(tornado.web.RequestHandler):

    async def post(self):
        db = self.application.database
        data = dict((k,v[-1] ) for k, v in self.request.arguments.items())
        for k, v in data.items(): data[k] = v.decode('utf-8')
        p = await db.posts.find_one({'count': int(data['post'])})
        report = {
            'ip': p['ip'],
            'post': int(data['post']),
            'reason': data['reason'],
            'date': datetime.datetime.utcnow(),
        }
        if not p['oppost']:
            report['url'] = '/' + p['board'] + '/thread/' + str(p['thread']) + '#' + str(p['count'])
        else:
            report['url'] = '/' + p['board'] + '/thread/' + str(p['count']) + '#' + str(p['count'])
        await db.reports.insert(report)
        response = {'ok': 'ok'}
        self.write(json.dumps(response))


# banning users using ajax; same stuff as with previous one
class AjaxBanHandler(tornado.web.RequestHandler):

    async def post(self):
        db = self.application.database
        data = dict((k,v[-1] ) for k, v in self.request.arguments.items())
        for k, v in data.items(): data[k] = v.decode('utf-8')
        p = await db.posts.find_one({'count': int(data['post'])})
        banned = await db.bans.find_one({'ip': p['ip']})
        if not banned:
            ban = {
                'ip': p['ip'],
                'ban_post': int(data['post']),
                'reason': data['reason'],
                'locked': False,
                'date': None,
                'date_of': datetime.datetime.utcnow(),
            }
            if data['lock'] == 'true':
                ban['locked'] = True
            if data['date'] != 'Never':
                ban['date'] = data['date']
            if not p['oppost']:
                ban['url'] = '/' + p['board'] + '/thread/' + str(p['thread']) + '#' + str(p['count'])
            else:
                ban['url'] = '/' + p['board'] + '/thread/' + str(p['count']) + '#' + str(p['count'])
            await db.bans.insert(ban)
            p['banned'] = True
            if ban['locked'] and p['oppost']:
                p['locked'] = True
            await update_db(db, p['count'], p)
        response = {'ok': 'ok'}
        self.write(json.dumps(response))


# admin main page
class AdminHandler(LoggedInHandler):

    async def get(self):
        if not self.current_user:
            self.redirect('/admin/login')
        else:
            boards_list = await self.application.database.boards.find({}).to_list(None)
            self.render('admin.html', boards_list=boards_list)


# creation of boards
class AdminBoardCreationHandler(LoggedInHandler):
    @ifadmin
    async def get(self):
        boards_list = await self.application.database.boards.find({}).to_list(None)
        self.render('admincreate.html', boards_list=boards_list)

    @ifadmin
    async def post(self):
        data = {}
        data['name'] = self.get_argument('name', '')
        data['short']= self.get_argument('short', '')
        data['username'] = self.get_argument('username', '')
        data['description'] = self.get_argument('description', '')
        data['thread_posts'] = int(self.get_argument('thread_posts', ''))
        data['thread_bump'] = int(self.get_argument('thread_bump', ''))
        data['thread_catalog'] = int(self.get_argument('thread_catalog', ''))
        data['country'] = 'country' in self.request.arguments
        data['custom'] = 'custom' in self.request.arguments
        data['postcount'] = 0
        data['mediacount'] = 0
        data['created'] = datetime.datetime.utcnow()
        db = self.application.database.boards
        await db.insert(data)
        self.redirect('/' + data['short'])


# login for admin; it's fucking awful since pass is in plaintext and that's only one of shitty things
# also cant use decorator here thus it's ugly as fuck
class AdminLoginHandler(LoggedInHandler):

    async def get(self):
        if not self.current_user:
            boards_list = await self.application.database.boards.find({}).to_list(None)
            self.render('admin_login.html', boards_list=boards_list)
        else:
            self.redirect('/admin')
            return

    async def post(self):
        password = self.get_argument('password')
        if password == _ib.ADMIN_PASS:
            self.set_secure_cookie('adminlogin', 'true')
            self.redirect('/admin')
        else:
            self.redirect('/')


# ban status for your ip
class BannedHandler(tornado.web.RequestHandler):
    async def get(self):
        db = self.application.database
        ip = await get_ip(self.request)
        ban = await db.bans.find_one({'ip':ip}) or None
        self.render('banned.html', ban=ban, boards_list=None)


# stats of boards for admins
class AdminStatsHandler(LoggedInHandler):
    @ifadmin
    async def get(self):
        boards = await self.application.database.boards.find({}).to_list(None)
        boards_list = await self.application.database.boards.find({}).to_list(None)
        self.render('admin_stats.html', boards=boards, boards_list=boards_list)


# you can view bans here
class AdminBannedHandler(LoggedInHandler):
    @ifadmin
    async def get(self):
        db = self.application.database
        bans = await db.bans.find({}).sort([('date', 1)]).to_list(None)
        boards_list = await db.boards.find({}).to_list(None)
        self.render('admin_banned.html', bans=bans, boards_list=boards_list)

    @ifadmin
    async def post(self):
        db = self.application.database
        ip = self.get_argument('ip')
        await db.bans.delete_one({'ip': ip})
        self.redirect('/admin/bans')


# you can view reports here
class AdminReportsHandler(LoggedInHandler):
    @ifadmin
    async def get(self):
        db = self.application.database
        reports = await db.reports.find({}).sort([('date', 1)]).to_list(None)
        boards_list = await db.boards.find({}).to_list(None)
        self.render('admin_reported.html', reports=reports, boards_list=boards_list)

    @ifadmin
    async def post(self):
        db = self.application.database
        ip = self.get_argument('ip')
        await db.reports.delete_one({'ip': ip})
        self.redirect('/admin/reports')

# constructs dictionary to insert into mongodb
async def makedata(db, subject, text, count, board, ip, oppost=False, thread=None, fo=None, f=None, filetype=None,
filedata=False, username=False, spoiler=False):
    data = {}
    data['ip'] = ip
    data['subject'] = subject
    data['text'] = text
    data['count'] = count
    data['board'] = board
    data['date'] = datetime.datetime.utcnow()
    data['oppost'] = oppost
    data['thread'] = thread
    data['banned'] = False
    data['replies'] = []
    data['country'] = ''
    data['trip'] = None
    b = await db.boards.find_one({'short': board})
    if b['country']:
        # workaround for localhost, replaces localhost with google ip (US)
        if ip == '127.0.0.1':
            ip = '172.217.20.206'
        data['country'] = gdbr.country(ip).country.iso_code
    if not b['custom']:
        if b['username'] != '':
            data['username'] = b['username']
        else:
            data['username'] = None
    else:
        if username and username != '':
            if '#' in username:
                uname, trip = username.split('#')
                data['username'] = uname
                data['trip'] = '!' + tripcode(trip)
            else:
                data['username'] = username
        else:
            if b['username'] != '':
                data['username'] = b['username']
            else:
                data['username'] = None
    if thread:
        t = await db.posts.find_one({'count': thread})
    if oppost:
        data['locked'] = False
        data['lastpost'] = datetime.datetime.utcnow()
        data['postcount'] = 0
        data['filecount'] = 0
    else:
        postcount = int(await db.posts.find({'thread': t['count']}).count())
        t['postcount'] = postcount + 1
        await update_db(db, t['count'], t)
    if f:
        b['mediacount'] = b['mediacount'] + 1
        data['original'] = fo
        if filetype == 'image':
            data['image'] = f
            data['video'] = None
            if not spoiler:
                data['thumb'] = await make_thumbnail(f)
            else:
                data['thumb'] = spoilered
        else:
            data['video'] = f
            data['image'] = None
            if not spoiler:
                data['thumb'] = await make_thumbnail(f)
            else:
                data['thumb'] = spoilered
        if not oppost:
            filecount = await db.posts.find({'thread': t['count'],
                                        'image': { '$ne': None }
                                        }).count() + await db.posts.find({'thread': t['count'],
                                                                    'video': {'$ne': None}}).count()
            t['filecount'] = filecount + 1
            await update_db(db, t['count'], t)
        if filedata:
            data['filedata'] = filedata
    else:
        data['image'] = data['video'] = None
    b['postcount'] = int(b['postcount']) + 1
    await update_db_b(db, b['short'], b)
    return data


# this is an ugly hack that works somehow
async def latest(db):
    try:
        return list(await db['posts'].find({}).sort('count', -1).to_list(None))[0]['count']
    except Exception as e:
        print(e)
        return 0


# checks if number of posts in thread exceeds whatever number you check it against
async def check_thread(db, thread, subj):
    return await db.posts.find({'thread': thread}).count() >= subj - 1


# updates one db entry by set parametres
async def update_db(db, count, variables):
    await db.posts.update_one(
        {'count': count},
        {
            '$set': variables
        }
    )


# for updating board data
async def update_db_b(db, short, variables):
    await db.boards.update_one(
        {'short': short},
        {
            '$set': variables
        }
    )


# deletes the threads that are inactive after there are too much threads
def schedule_check(app):
    next_time = datetime.timedelta(0, _ib.CHECK_TIMEOUT)
    @tornado.gen.coroutine
    def task():
        # delete all threads except first N, sorted by bumps
        db = app.database
        boards = yield db.boards.find({}).to_list(None)
        try:
            for board in boards:
                threads = yield db.posts.find({'oppost': True,
                                        'board': board['short']}).sort('lastpost', 1).to_list(None)
                if not len(threads) <= board['thread_catalog']:
                    threads = threads[:(threads.count(None) - board['thread_catalog'])]
                    for thread in threads:
                        if thread['thumb'] != thumb_def and thread['thumb'] != spoilered:
                            os.remove(thread['thumb'])
                        if thread['video']:
                            os.remove(thread['video'])
                        if thread['image']:
                            os.remove(thread['image'])
                        posts = yield db.posts.find({'thread': thread['count']}).to_list(None)
                        for post in posts:
                            if post['video']:
                                if os.path.isfile(post['video']):
                                    os.remove(post['video'])
                            if post['image']:
                                if os.path.isfile(post['image']):
                                    os.remove(post['image'])
                            if post['thumb'] != thumb_def and post['thumb'] != spoilered:
                                if os.path.isfile(post['thumb']):
                                    os.remove(post['thumb'])
                        yield db.posts.delete_many({'thread': thread['count']})
                        yield db.posts.remove({'count': thread['count']})
        except Exception as e:
            print(e)
    def wrapper():
        executor.submit(task)
        schedule_check(app)
    tornado.ioloop.IOLoop.current().add_timeout(next_time, wrapper)


async def get_ip(req):
    x_real_ip = req.headers.get('X-Real-IP')
    return x_real_ip or req.remote_ip


async def is_banned(db, ip):
    ban = await db.bans.find_one({'ip': ip})
    if ban:
        if ban['date']:
            expires = datetime.datetime.strptime(ban['date'], "%d-%m-%Y")
            if expires > datetime.datetime.today():
                return True
            else:
                await db.bans.delete_one({'ip': ip})
        else:
            return True
    return False


def get_replies(text):
    replies = []
    text_list = re.split(r'(\s+)', text)
    for t in text_list:
        x = re.compile(r'(>>\d+)').match(t)
        if x:
            number = x.group(1)
            replies.append(number[2:])
    return replies


class Application(tornado.web.Application):

    def __init__(self):
        handlers = [
            (r'/$', IndexHandler),
            (r'/admin/?', AdminHandler),
            (r'/banned/?', BannedHandler),
            (r'/flags/(.*)/?', tornado.web.StaticFileHandler, {'path': 'flags'}),
            (r'/(\w+)/?', BoardHandler),
            (r'/(\w+)/thread/(\d+)/?', ThreadHandler),
            (r'/(\w+)/thread/(\d+)/json/?', JsonThreadHandler),
            (r'/admin/login/?', AdminLoginHandler),
            (r'/admin/create/?', AdminBoardCreationHandler),
            (r'/admin/stats/?', AdminStatsHandler),
            (r'/admin/bans/?', AdminBannedHandler),
            (r'/admin/reports/?', AdminReportsHandler),
            (r'/uploads/(.*)/?', tornado.web.StaticFileHandler, {'path': 'uploads'}),
            (r'/ajax/remove/?', AjaxDeleteHandler),
            (r'/ajax/ban/?', AjaxBanHandler),
            (r'/ajax/report/?', AjaxReportHandler),
        ]

        settings = {
            'ui_modules': uimodules,
            'template_path': 'templates',
            'static_path': 'static',
            'xsrf_cookies': True,
            'cookie_secret': "__TODO:_GENERATE_YOUR_OWN_RANDOM_VALUE_HERE__",
        }

        self.con = motor.motor_tornado.MotorClient('localhost', 27017)
        self.database = self.con['imageboard']
        tornado.web.Application.__init__(self, handlers, **settings)


def main():
    check_uploads()
    tornado.options.parse_command_line()
    application = Application()
    http_server = tornado.httpserver.HTTPServer(application, max_buffer_size=_ib.MAX_FILESIZE)
    http_server.listen(options.port)
    schedule_check(application)
    tornado.ioloop.IOLoop.instance().start()


if __name__ == '__main__':
    main()
