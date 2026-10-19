package com.hmdp.controller;

import com.hmdp.dto.Result;
import com.hmdp.entity.Event;
import com.hmdp.entity.EventGroup;
import com.hmdp.service.IEventGroupService;
import com.hmdp.service.IEventService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import javax.annotation.Resource;

/**
 * 读路径：赛事详情走多级缓存，组别余量走 Redis 实时值。
 */
@RestController
@RequestMapping("/event")
public class EventController {

    @Resource
    private IEventService eventService;

    @Resource
    private IEventGroupService eventGroupService;

    @GetMapping("/list")
    public Result list(@RequestParam(value = "page", defaultValue = "1") int page) {
        return eventService.listByPage(page);
    }

    @GetMapping("/{id}")
    public Result detail(@PathVariable("id") Long id) {
        return eventService.queryById(id);
    }

    @GetMapping("/{id}/detail")
    public Result detailWithGroups(@PathVariable("id") Long id) {
        return eventService.queryDetail(id);
    }

    @GetMapping("/{id}/groups")
    public Result groups(@PathVariable("id") Long id) {
        return Result.ok(eventGroupService.listByEventId(id));
    }

    @PostMapping
    public Result create(@RequestBody Event event) {
        return eventService.create(event);
    }

    @PutMapping
    public Result update(@RequestBody Event event) {
        return eventService.update(event);
    }

    @PostMapping("/group")
    public Result createGroup(@RequestBody EventGroup group) {
        return eventGroupService.createGroup(group);
    }
}
