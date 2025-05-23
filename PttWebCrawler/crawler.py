# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import print_function
from collections import defaultdict

import os
import re
import sys
import json
import requests
import argparse
import datetime
import time
import codecs
import psycopg2
from bs4 import BeautifulSoup
from six import u

__version__ = '1.0'

# if python 2, disable verify flag in requests.get()
VERIFY = True
if sys.version_info[0] < 3:
    VERIFY = False
    requests.packages.urllib3.disable_warnings()

DATE_FORMAT = "%a %b %d %H:%M:%S %Y"
PTT = 6

def insert_to_timescaledb(ptt_stat):
    # Connect to TimescaleDB
    conn = psycopg2.connect(
        host="192.168.31.13",
        database="stockpro",
        user="root",
        password="Pluto2005"
    )

    cur = conn.cursor()

    # Insert data into daily_scalar_values table
    insert_query = """
    INSERT INTO daily_scalar_values (record_date, security_id, value, last_updated_at)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (record_date, security_id) DO UPDATE SET 
        value = EXCLUDED.value,
        last_updated_at = EXCLUDED.last_updated_at
    WHERE 
        EXCLUDED.value > daily_scalar_values.value;
    """

    # Get current timestamp for last_updated_at
    now = datetime.datetime.now()
    
    # Loop through each date and article count in the ptt_stat dictionary
    for date_str, article_count in ptt_stat.items():
        try:
            # Convert string date to datetime object
            record_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
            
            # Insert data into TimescaleDB
            # security_id is set to PTT (6) as defined at the top of the file
            cur.execute(insert_query, (record_date, PTT, article_count, now))
            print(f"Inserted data for {date_str}: {article_count} articles")
        except Exception as e:
            print(f"Error inserting data for {date_str}: {e}")

    # Commit the transaction and close the connection
    conn.commit()
    cur.close()
    conn.close()
    print("Data successfully inserted into TimescaleDB")

class PttWebCrawler(object):
    PTT_URL = 'https://www.ptt.cc'

    """docstring for PttWebCrawler"""
    def __init__(self, cmdline=None, as_lib=False):
        parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter, description='''
            A crawler for the web version of PTT, the largest online community in Taiwan.
            Input: board name and page indices (or article ID)
            Output: BOARD_NAME-START_INDEX-END_INDEX.json (or BOARD_NAME-ID.json)
        ''')
        parser.add_argument('-b', metavar='BOARD_NAME', help='Board name', required=True)
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('-i', metavar=('START_INDEX', 'END_INDEX'), type=int, nargs=2, help="Start and end index")
        group.add_argument('-a', metavar='ARTICLE_ID', help="Article ID")
        parser.add_argument('-sp', '--stockpro', action='store_true', help="Stockpro mode: to count number of articles per day.")
        parser.add_argument('-ar', '--author', action='append', help="Filter articles by author name (can be specified multiple times)")
        parser.add_argument('-v', '--version', action='version', version='%(prog)s ' + __version__)

        if not as_lib:
            if cmdline:
                args = parser.parse_args(cmdline)
            else:
                args = parser.parse_args()
            board = args.b
            authors = args.author if hasattr(args, 'author') and args.author else None
            if args.i:
                start = args.i[0]

                if args.i[0] < 0:
                    start = self.getLastPage(board)+args.i[0]

                if args.i[1] == -1:
                    end = self.getLastPage(board)
                else:
                    end = args.i[1]

                if args.stockpro:
                    self.stockpro_mode(start, end, board)
                else:
                    self.parse_articles(start, end, board, authors=authors)
            else:  # args.a
                article_id = args.a
                self.parse_article(article_id, board, authors=authors)

    def stockpro_mode(self, start, end, board):
        ptt_stat = defaultdict(int)

        start_time = time.perf_counter()
        print('Start: ', start, 'Last: ', end)

        self.parse_articles_stockpro(start, end, board, path='.', timeout=3, stockpro_data=ptt_stat)
        insert_to_timescaledb(ptt_stat)
        end_time = time.perf_counter()
        elapsed_time = end_time - start_time

        print('Start: ', start, 'Last: ', end)
        print(f"Elapsed time: {elapsed_time:.4f} seconds")
        print('PTT Stock data:', dict(ptt_stat))  # Convert defaultdict to regular dict for printing

    def parse_articles_stockpro(self, start, end, board, path='.', timeout=3, stockpro_data=None):
        filename = board + '-' + str(start) + '-' + str(end) + '.json'
        filename = os.path.join(path, filename)
        for i in range(end-start+1):
            index = start + i
            print('Processing index:', str(index))
            resp = requests.get(
                url = self.PTT_URL + '/bbs/' + board + '/index' + str(index) + '.html',
                cookies={'over18': '1'}, verify=VERIFY, timeout=timeout
            )
            print('Processing url:', resp.url)
            if resp.status_code != 200:
                print('invalid url:', resp.url)
                continue
            soup = BeautifulSoup(resp.text, 'html.parser')
            divs = soup.find_all("div", "r-ent")
            for div in divs:
                try:
                    # ex. link would be <a href="/bbs/PublicServan/M.1127742013.A.240.html">Re: [問題] 職等</a>
                    href = div.find('a')['href']
                    link = self.PTT_URL + href
                    article_id = re.sub('\.html', '', href.split('/')[-1])
                    result = self.parse_stockpro(link, article_id, stockpro_data)
                    # result = self.parse_new(link, article_id, board, filter_authors=None)
                    if result:  # Only store if the article matches the author filter
                        if div == divs[-1] and i == end-start:  # last div of last page
                            self.store(filename, result, 'a')
                        else:
                            self.store(filename, result + ',\n', 'a')
                except:
                    pass
            time.sleep(0.1)

        return filename

    def parse_articles(self, start, end, board, authors=None, path='.', timeout=3):
        filename = board + '-' + str(start) + '-' + str(end) + '.json'
        filename = os.path.join(path, filename)
        self.store(filename, u'{"articles": [', 'w')
        for i in range(end-start+1):
            index = start + i
            print('Processing index:', str(index))
            resp = requests.get(
                url = self.PTT_URL + '/bbs/' + board + '/index' + str(index) + '.html',
                cookies={'over18': '1'}, verify=VERIFY, timeout=timeout
            )
            if resp.status_code != 200:
                print('invalid url:', resp.url)
                continue
            soup = BeautifulSoup(resp.text, 'html.parser')
            divs = soup.find_all("div", "r-ent")
            for div in divs:
                try:
                    # ex. link would be <a href="/bbs/PublicServan/M.1127742013.A.240.html">Re: [問題] 職等</a>
                    href = div.find('a')['href']
                    link = self.PTT_URL + href
                    article_id = re.sub('\.html', '', href.split('/')[-1])
                    result = self.parse(link, article_id, board, filter_authors=authors)
                    if result:  # Only store if the article matches the author filter
                        if div == divs[-1] and i == end-start:  # last div of last page
                            self.store(filename, result, 'a')
                        else:
                            self.store(filename, result + ',\n', 'a')
                except:
                    pass
            time.sleep(0.1)
        self.store(filename, u']}', 'a')
        return filename

    def parse_article(self, article_id, board, authors=None, path='.'):
        link = self.PTT_URL + '/bbs/' + board + '/' + article_id + '.html'
        filename = board + '-' + article_id + '.json'
        filename = os.path.join(path, filename)
        result = self.parse(link, article_id, board, filter_authors=authors)
        if result:  # Only store if the article matches the author filter
            self.store(filename, result, 'w')
            return filename
        else:
            print(f"Article {article_id} does not match the author filter.")
            return None

    @staticmethod
    def parse_stockpro(link, article_id, stockpro_data):
        # print('Processing article:', article_id)
        resp = requests.get(url=link, cookies={'over18': '1'}, verify=VERIFY, timeout=3)
        if resp.status_code != 200:
            print('invalid url:', resp.url)

        soup = BeautifulSoup(resp.text, 'html.parser')
        main_content = soup.find(id="main-content")
        metas = main_content.select('div.article-metaline')
        author = ''
        title = ''
        date = ''

        if metas:
            date = metas[2].select('span.article-meta-value')[0].string if metas[2].select('span.article-meta-value')[0] else date
            title = metas[1].select('span.article-meta-value')[0].string if metas[1].select('span.article-meta-value')[0] else title

            if "公告" in title:
                pass
            else:
                parsed_date = datetime.datetime.strptime(date, DATE_FORMAT)
                formatted_date = parsed_date.strftime("%Y-%m-%d")
                stockpro_data[formatted_date] += 1
                # print("Date: ", formatted_date, ", Title: ", title)

    @staticmethod
    def parse(link, article_id, board, filter_authors=None, timeout=3):
        print('Processing article:', article_id)
        resp = requests.get(url=link, cookies={'over18': '1'}, verify=VERIFY, timeout=timeout)
        if resp.status_code != 200:
            print('invalid url:', resp.url)
            return json.dumps({"error": "invalid url"}, sort_keys=True, ensure_ascii=False)
        soup = BeautifulSoup(resp.text, 'html.parser')
        main_content = soup.find(id="main-content")
        metas = main_content.select('div.article-metaline')
        author = ''
        title = ''
        date = ''
        if metas:
            author = metas[0].select('span.article-meta-value')[0].string if metas[0].select('span.article-meta-value')[0] else author
            title = metas[1].select('span.article-meta-value')[0].string if metas[1].select('span.article-meta-value')[0] else title
            date = metas[2].select('span.article-meta-value')[0].string if metas[2].select('span.article-meta-value')[0] else date

            # remove meta nodes
            for meta in metas:
                meta.extract()
            for meta in main_content.select('div.article-metaline-right'):
                meta.extract()

        # remove and keep push nodes
        pushes = main_content.find_all('div', class_='push')
        for push in pushes:
            push.extract()

        try:
            ip = main_content.find(string=re.compile(u'※ 發信站:'))
            ip = re.search('[0-9]*\.[0-9]*\.[0-9]*\.[0-9]*', ip).group()
        except:
            ip = "None"

        # 移除 '※ 發信站:' (starts with u'\u203b'), '◆ From:' (starts with u'\u25c6'), 空行及多餘空白
        # 保留英數字, 中文及中文標點, 網址, 部分特殊符號
        filtered = [ v for v in main_content.stripped_strings if v[0] not in [u'※', u'◆'] and v[:2] not in [u'--'] ]
        expr = re.compile(u(r'[^\u4e00-\u9fa5\u3002\uff1b\uff0c\uff1a\u201c\u201d\uff08\uff09\u3001\uff1f\u300a\u300b\s\w:/-_.?~%()]'))
        for i in range(len(filtered)):
            filtered[i] = re.sub(expr, '', filtered[i])

        filtered = [_f for _f in filtered if _f]  # remove empty strings
        filtered = [x for x in filtered if article_id not in x]  # remove last line containing the url of the article
        content = ' '.join(filtered)
        content = re.sub(r'(\s)+', ' ', content)
        # print 'content', content

        # push messages
        p, b, n = 0, 0, 0
        messages = []
        for push in pushes:
            if not push.find('span', 'push-tag'):
                continue
            push_tag = push.find('span', 'push-tag').string.strip(' \t\n\r')
            push_userid = push.find('span', 'push-userid').string.strip(' \t\n\r')
            # if find is None: find().strings -> list -> ' '.join; else the current way
            push_content = push.find('span', 'push-content').strings
            push_content = ' '.join(push_content)[1:].strip(' \t\n\r')  # remove ':'
            push_ipdatetime = push.find('span', 'push-ipdatetime').string.strip(' \t\n\r')
            messages.append( {'push_tag': push_tag, 'push_userid': push_userid, 'push_content': push_content, 'push_ipdatetime': push_ipdatetime} )
            if push_tag == u'推':
                p += 1
            elif push_tag == u'噓':
                b += 1
            else:
                n += 1

        # count: 推噓文相抵後的數量; all: 推文總數
        message_count = {'all': p+b+n, 'count': p-b, 'push': p, 'boo': b, "neutral": n}

        # print 'msgs', messages
        # print 'mscounts', message_count

        # Filter by author if specified
        if filter_authors is not None:
            for filter_author in filter_authors:
                if filter_author.lower() in author.lower():
                    break
            else:
                return None

        # json data
        data = {
            'url': link,
            'board': board,
            'article_id': article_id,
            'article_title': title,
            'author': author,
            'date': date,
            'content': content,
            'ip': ip,
            'message_count': message_count,
            'messages': messages
        }
        # print 'original:', d
        return json.dumps(data, sort_keys=True, ensure_ascii=False)

    @staticmethod
    def getLastPage(board, timeout=3):
        content = requests.get(
            url= 'https://www.ptt.cc/bbs/' + board + '/index.html',
            cookies={'over18': '1'}, timeout=timeout
        ).content.decode('utf-8')
        first_page = re.search(r'href="/bbs/\w+/index(\d+).html">&lsaquo;', content)
        if first_page is None:
            return 1
        return int(first_page.group(1)) + 1

    @staticmethod
    def store(filename, data, mode):
        with codecs.open(filename, mode, encoding='utf-8') as f:
            f.write(data)

    @staticmethod
    def get(filename, mode='r'):
        with codecs.open(filename, mode, encoding='utf-8') as f:
            return json.load(f)

if __name__ == '__main__':
    c = PttWebCrawler()
