# -*- coding: UTF-8 -*-
from flask import current_app, copy_current_request_context
from datetime import datetime
from urllib.parse import urljoin
from gevent.pool import Pool as GeventPool
from gevent.queue import Queue as GeventQueue, Empty as GeventEmpty

from ... import db
from ..models.douyu import DouyuChannel, DouyuRoom, DouyuChannelData, DouyuRoomData
from .. import base_headers

import requests

__all__ = ['settings', 'crawl_task', 'request_headers',
           'crawl_channel_list', 'crawl_room_list', 'search_room_list', 'crawl_room_all', 'crawl_room']

CHANNEL_LIST_API = 'http://www.douyu.com/api/RoomApi/game'
ROOM_LIST_API = 'http://www.douyu.com/api/v1/live/{}?offset={}&limit={}'
ROOM_API = 'http://www.douyu.com/api/RoomApi/room/{}'
request_headers = dict(base_headers, Host='www.douyu.com', Referer='http://www.douyu.com')
settings = {
    'code': 'douyu',
    'name': '斗鱼',
    'description': '斗鱼-全民直播平台',
    'url': 'http://www.douyu.com',
    'image_url': 'http://staticlive.douyutv.com/common/douyu/images/logo_zb.png',
    'order_int': 1,
}


def crawl_task(self):
    self.crawl_channel_list()
    self.crawl_room_list([channel for channel in list(DouyuChannel.query.filter_by(site=self.site))])


def crawl_channel_list(self):
    current_app.logger.info('调用频道接口:{}'.format(CHANNEL_LIST_API))
    resp = self._get_response(CHANNEL_LIST_API)
    if resp.status_code != requests.codes.ok:
        current_app.logger.error('调用接口{}失败: 状态{}'.format(CHANNEL_LIST_API, resp.status_code))
        raise ValueError('调用接口{}失败: 状态{}'.format(CHANNEL_LIST_API, resp.status_code))
    respjson = resp.json()
    if respjson['error'] != 0:
        current_app.logger.error('调用接口失败: 返回错误结果{}'.format(respjson))
        raise ValueError('返回错误结果{}'.format(respjson))
    self.site.channels.update({'valid': False})
    for channel_json in respjson['data']:
        channel = DouyuChannel.query.filter_by(site=self.site, officeid=channel_json['cate_id']).one_or_none()
        if not channel:
            channel = DouyuChannel(officeid=channel_json['cate_id'], site=self.site)
            current_app.logger.info('新增频道 {}:{}'.format(channel_json['game_name'], channel_json['game_url']))
        else:
            current_app.logger.info('更新频道 {}:{}'.format(channel_json['game_name'], channel_json['game_url']))
        channel.url = channel_json['game_url']
        channel.name = channel_json['game_name']
        channel.code = channel_json['short_name']
        channel.image_url = channel_json['game_src']
        channel.icon_url = channel_json['game_icon']
        channel.valid = True
        db.session.add(channel)
    self.site.crawl_date = datetime.now()
    db.session.add(self.site)
    db.session.commit()


def crawl_room_list(self, channel_list):
    gpool = GeventPool(5)
    gqueue = GeventQueue()
    for channel in channel_list:
        channel.rooms.update({'openstatus': False})
        db.session.commit()
        gpool.spawn(copy_current_request_context(self.search_room_list), channel, gqueue)
    while not gqueue.empty() or gpool.free_count() < gpool.size:
        try:
            restype, channel_res, resjson = gqueue.get(timeout=1)
        except GeventEmpty:
            current_app.logger.info('等待队列结果...')
            continue
        if restype == 'room_list':
            for room_json in resjson:
                room = DouyuRoom.query.filter_by(officeid=room_json['room_id']).one_or_none()
                if not room:
                    room = DouyuRoom(officeid=room_json['room_id'])
                    current_app.logger.info('新增房间 {}:{}'.format(room_json['room_id'], room_json['room_name']))
                else:
                    pass
                    #current_app.logger.debug('更新房间 {}:{}'.format(room_json['room_id'], room_json['room_name']))
                room.channel = channel_res
                room.name = room_json['room_name']
                room.image_url = room_json['room_src']
                room.owner_name = room_json['nickname']
                room.owner_uid = room_json['owner_uid']
                room.owner_avatar = room_json['avatar']
                room.spectators = room_json['online']
                room.crawl_date = datetime.now()
                room.openstatus = True
                if 'fans' in room_json:
                    room.followers = int(room_json['fans']) if room_json['fans'].isdecimal() else 0
                    room.url = urljoin(self.site.url, room_json['url'])
                else:
                    room.url = room_json['url']
                db.session.add(room)
                room_data = DouyuRoomData(room=room, spectators=room.spectators, followers=room.followers)
                db.session.add(room_data)
        elif restype == 'channel':
            db.session.add(channel_res)
            db.session.add(resjson)
        db.session.commit()


def search_room_list(self, channel, gqueue):
    current_app.logger.info('开始扫描频道房间 {}: {}'.format(channel.name, channel.url))
    crawl_offset, crawl_limit = 0, 100
    crawl_room_count = 0
    while True:
        roomjsonlen = 0
        for i in range(3):
            requrl = ROOM_LIST_API.format(channel.officeid, crawl_offset, crawl_limit)
            resp = self._get_response(requrl)
            if not resp or resp.status_code != requests.codes.ok:
                current_app.logger.error('调用接口 {} 失败: 状态{}'.format(requrl, resp.status_code if resp else ''))
                continue
            try:
                respjson = resp.json()
            except ValueError:
                current_app.logger.error('调用接口{}失败: 内容解析json失败'.format(requrl))
                continue
            if respjson['error'] != 0:
                current_app.logger.error('调用接口{}失败: 返回错误结果{}'.format(requrl, respjson))
                continue
            roomjsonlen = len(respjson['data'])
            gqueue.put(('room_list', channel, respjson['data']))
            break
        else:
            error_msg = '扫描频道房间超过次数失败 {}: {}'.format(channel.name, channel.url)
            current_app.logger.error(error_msg)
            gqueue.put(('error', channel, error_msg))
            raise ValueError(error_msg)
        crawl_room_count += roomjsonlen
        if roomjsonlen < crawl_limit:
            if roomjsonlen + 1 == crawl_limit:
                crawl_offset += crawl_limit - 1
            else:
                break
        else:
            crawl_offset += crawl_limit
    channel.room_range = crawl_room_count - channel.room_total
    channel.room_total = crawl_room_count
    channel.crawl_date = datetime.now()
    channel_data = DouyuChannelData(channel=channel, room_total=channel.room_total)
    gqueue.put(('channel', channel, channel_data))
    current_app.logger.info('结束扫描频道房间 {}: {}'.format(channel.name, channel.url))


def crawl_room_all(self):
    gpool = GeventPool(5)
    gqueue = GeventQueue()
    for room in list(DouyuRoom.query.filter_by(openstatus=True)):
        gpool.spawn(copy_current_request_context(self.crawl_room), room, gqueue)
    while not gqueue.empty() or gpool.free_count() < gpool.size:
        try:
            restype, room, resjson = gqueue.get(timeout=1)
        except GeventEmpty:
            continue
        if restype == 'room':
            current_app.logger.info('更新房间详细信息 {}:{}'.format(room.officeid, room.name))
            db.session.add(room)
            db.session.add(resjson)
        db.session.commit()


def crawl_room(self, room, gqueue):
    room_requrl = ROOM_API.format(room.officeid)
    current_app.logger.info('开始扫描房间详细信息: {}'.format(room_requrl))
    room_resp = self._get_response(room_requrl)
    if not room_resp or room_resp.status_code != requests.codes.ok:
        error_msg = '调用接口 {} 失败: 状态{}'.format(room_requrl, room_resp.status_code if room_resp else '')
        current_app.logger.error(error_msg)
        gqueue.put(('error', room, error_msg))
        raise ValueError(error_msg)
    room_respjson = room_resp.json()
    if room_respjson['error'] != 0:
        error_msg = '调用房间接口{}失败: 返回错误结果{}'.format(room_requrl, room_respjson)
        current_app.logger.error(error_msg)
        gqueue.put(('error', room, error_msg))
        raise ValueError(error_msg)
    room_respjson = room_respjson['data']
    room.name = room_respjson['room_name']
    room.image_url = room_respjson['room_thumb']
    room.owner_name = room_respjson['owner_name']
    room.owner_avatar = room_respjson['avatar']
    room.spectators = room_respjson['online']
    room.openstatus = room_respjson['room_status'] == '1'
    room.followers = int(room_respjson['fans_num']) if room_respjson['fans_num'].isdecimal() else 0
    room.weight = room_respjson['owner_weight']
    if room.weight.endswith('t'):
        room.weight_int = int(float(room.weight[:-1]) * 1000 * 1000)
    elif room.weight.endswith('kg'):
        room.weight_int = int(float(room.weight[:-2]) * 1000)
    elif room.weight.endswith('g'):
        room.weight_int = int(room.weight[:-1])
    room.crawl_date = datetime.now()
    room.start_time = datetime.strptime(room_respjson['start_time'], '%Y-%m-%d %H:%M')
    room_data = DouyuRoomData(room=room, spectators=room.spectators, followers=room.followers,
                              weight=room.weight, weight_int=room.weight_int)
    gqueue.put(('room', room, room_data))