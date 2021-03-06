# encoding: utf-8

"""
.. codeauthor:: Tsuyoshi Hombashi <tsuyoshi.hombashi@gmail.com>
"""

from __future__ import absolute_import, unicode_literals

import copy

import logbook
import simplesqlite
import subprocrunner
import typepy
from typepy import Integer

from .._common import is_execute_tc_command
from .._const import Tc, TcSubCommand, TrafficDirection
from .._error import NetworkInterfaceNotFoundError
from .._iptables import IptablesMangleController
from .._network import is_anywhere_network
from .._tc_command_helper import get_tc_base_command, run_tc_show
from ._class import TcClassParser
from ._filter import TcFilterParser
from ._qdisc import TcQdiscParser


class TcShapingRuleParser(object):
    @property
    def con(self):
        return self.__con

    @property
    def device(self):
        return self.__device

    @property
    def ifb_device(self):
        return self.__ifb_device

    def __init__(self, device, ip_version, tc_command_output, logger):
        self.__device = device
        self.__ip_version = ip_version
        self.__tc_command_output = tc_command_output
        self.__logger = logger

        self.clear()
        self.__ifb_device = self.__get_ifb_from_device()

        self.__iptables_ctrl = IptablesMangleController(True, ip_version)

    def clear(self):
        self.__con = simplesqlite.connect_sqlite_memdb()
        self.__filter_parser = TcFilterParser(self.__con, self.__ip_version)
        self.__parsed_mappings = {}

    def get_tc_parameter(self):
        return {
            self.device: {
                TrafficDirection.OUTGOING: self.__get_shaping_rule(self.device),
                TrafficDirection.INCOMING: self.__get_shaping_rule(self.ifb_device),
            }
        }

    def get_outgoing_tc_filter(self):
        return self.__parse_tc_filter(self.device)

    def get_incoming_tc_filter(self):
        if not self.ifb_device:
            return []

        return self.__parse_tc_filter(self.ifb_device)

    def parse(self):
        self.__parse_device(self.device)
        self.__parse_device(self.ifb_device)

    def __parse_device(self, device):
        if not device:
            return

        if self.__parsed_mappings.get(device):
            return

        self.__parse_tc_class(device)
        self.__parse_tc_filter(device)
        self.__parse_tc_qdisc(device)

        self.__parsed_mappings[device] = True

    def __get_ifb_from_device(self):
        if not is_execute_tc_command(self.__tc_command_output):
            return None

        filter_runner = subprocrunner.SubprocessRunner(
            "{:s} show dev {:s} root".format(get_tc_base_command(TcSubCommand.FILTER), self.device),
            error_log_level=logbook.NOTSET,
            dry_run=False,
        )
        if filter_runner.run() != 0 and filter_runner.stderr.find("Cannot find device") != -1:
            raise NetworkInterfaceNotFoundError(device=self.device)

        return self.__filter_parser.parse_incoming_device(filter_runner.stdout)

    def __get_filter_key(self, filter_param):
        src_network_format = Tc.Param.SRC_NETWORK + "={:s}"
        dst_network_format = Tc.Param.DST_NETWORK + "={:s}"
        protocol_format = Tc.Param.PROTOCOL + "={:s}"
        key_item_list = []

        if Tc.Param.HANDLE in filter_param:
            handle = filter_param.get(Tc.Param.HANDLE)
            Integer(handle).validate()
            handle = int(handle)

            for mangle in self.__iptables_ctrl.parse():
                if mangle.mark_id != handle:
                    continue

                key_item_list.append(dst_network_format.format(mangle.destination))
                if typepy.is_not_null_string(mangle.source):
                    key_item_list.append("{:s}={:s}".format(Tc.Param.SRC_NETWORK, mangle.source))
                key_item_list.append(protocol_format.format(mangle.protocol))

                break
            else:
                raise ValueError("mangle mark not found: {}".format(mangle))
        else:
            src_network = filter_param.get(Tc.Param.SRC_NETWORK)
            if typepy.is_not_null_string(src_network) and not is_anywhere_network(
                src_network, self.__ip_version
            ):
                key_item_list.append(src_network_format.format(src_network))

            dst_network = filter_param.get(Tc.Param.DST_NETWORK)
            if typepy.is_not_null_string(dst_network) and not is_anywhere_network(
                dst_network, self.__ip_version
            ):
                key_item_list.append(dst_network_format.format(dst_network))

            src_port = filter_param.get(Tc.Param.SRC_PORT)
            if Integer(src_port).is_type():
                port_format = Tc.Param.SRC_PORT + "={:d}"
                key_item_list.append(port_format.format(src_port))

            dst_port = filter_param.get(Tc.Param.DST_PORT)
            if Integer(dst_port).is_type():
                port_format = Tc.Param.DST_PORT + "={:d}"
                key_item_list.append(port_format.format(dst_port))

            protocol = filter_param.get(Tc.Param.PROTOCOL)
            if typepy.is_not_null_string(protocol):
                key_item_list.append(protocol_format.format(protocol))

        return ", ".join(key_item_list)

    def __get_shaping_rule(self, device):
        from simplesqlite.query import Where

        if typepy.is_null_string(device):
            return {}

        self.__parse_device(device)
        where_query = Where(Tc.Param.DEVICE, device)

        try:
            class_param_list = self.__con.select_as_dict(
                table_name=TcSubCommand.CLASS.value, where=where_query
            )
        except simplesqlite.TableNotFoundError:
            class_param_list = []

        try:
            filter_param_list = self.__con.select_as_dict(
                table_name=TcSubCommand.FILTER.value, where=where_query
            )
        except simplesqlite.TableNotFoundError:
            filter_param_list = []

        try:
            qdisc_param_list = self.__con.select_as_dict(
                table_name=TcSubCommand.QDISC.value, where=where_query
            )
        except simplesqlite.TableNotFoundError:
            qdisc_param_list = []

        shaping_rule_mapping = {}

        for filter_param in filter_param_list:
            self.__logger.debug("{:s} param: {}".format(TcSubCommand.FILTER, filter_param))
            shaping_rule = {}

            filter_key = self.__get_filter_key(filter_param)
            if typepy.is_null_string(filter_key):
                self.__logger.debug("empty filter key: {}".format(filter_param))
                continue

            for qdisc_param in qdisc_param_list:
                self.__logger.debug("{:s} param: {}".format(TcSubCommand.QDISC, qdisc_param))

                if qdisc_param.get(Tc.Param.PARENT) not in (
                    filter_param.get(Tc.Param.FLOW_ID),
                    filter_param.get(Tc.Param.CLASS_ID),
                ):
                    continue

                shaping_rule[Tc.Param.FILTER_ID] = filter_param.get(Tc.Param.FILTER_ID)
                # shaping_rule[Tc.Param.PRIORITY] = filter_param.get(
                #    Tc.Param.PRIORITY)
                shaping_rule.update(
                    self.__strip_param(
                        qdisc_param, [Tc.Param.DEVICE, Tc.Param.PARENT, Tc.Param.HANDLE]
                    )
                )

            for class_param in class_param_list:
                self.__logger.debug("{:s} param: {}".format(TcSubCommand.CLASS, class_param))

                if class_param.get(Tc.Param.CLASS_ID) not in (
                    filter_param.get(Tc.Param.FLOW_ID),
                    filter_param.get(Tc.Param.CLASS_ID),
                ):
                    continue

                shaping_rule[Tc.Param.FILTER_ID] = filter_param.get(Tc.Param.FILTER_ID)
                # shaping_rule[Tc.Param.PRIORITY] = filter_param.get(
                #    Tc.Param.PRIORITY)
                shaping_rule.update(
                    self.__strip_param(class_param, [Tc.Param.DEVICE, Tc.Param.CLASS_ID])
                )

            if not shaping_rule:
                self.__logger.debug("shaping rule not found for '{}'".format(filter_param))
                continue

            self.__logger.debug("shaping rule found: {} {}".format(filter_key, shaping_rule))

            shaping_rule_mapping[filter_key] = shaping_rule

        return shaping_rule_mapping

    def __parse_tc_qdisc(self, device):
        return TcQdiscParser(self.__con).parse(
            device, run_tc_show(TcSubCommand.QDISC, device, self.__tc_command_output)
        )

    def __parse_tc_filter(self, device):
        return self.__filter_parser.parse(
            device, run_tc_show(TcSubCommand.FILTER, device, self.__tc_command_output)
        )

    def __parse_tc_class(self, device):
        return TcClassParser(self.__con).parse(
            device, run_tc_show(TcSubCommand.CLASS, device, self.__tc_command_output)
        )

    @staticmethod
    def __strip_param(params, strip_param_list):
        work_params = copy.deepcopy(params)

        for strip_param in strip_param_list:
            try:
                del work_params[strip_param]
            except KeyError:
                pass

        return work_params
