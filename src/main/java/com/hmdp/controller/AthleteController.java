package com.hmdp.controller;

import com.hmdp.dto.Result;
import com.hmdp.entity.Athlete;
import com.hmdp.service.IAthleteService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import javax.annotation.Resource;

@RestController
@RequestMapping("/athlete")
public class AthleteController {

    @Resource
    private IAthleteService athleteService;

    @GetMapping("/me")
    public Result me() {
        return athleteService.queryMine();
    }

    @PostMapping
    public Result save(@RequestBody Athlete athlete) {
        return athleteService.saveProfile(athlete);
    }
}
