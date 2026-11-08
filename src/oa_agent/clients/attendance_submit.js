// 在隔离的 OA 页面内执行；所有业务值由 evaluate 的参数传入。
async ({year, month, records, corrections, choices}) => {
    const table = control.info_v.$refs.tbl;
    if (FlowCtlData.nodeThisData[0].NODEID !== "flowStart" ||
        form_data.setfile.formid !== "abnormalattendapp") return false;
    const remarkColumns = table.list_title.filter(column => column.name === "备注");
    if (remarkColumns.length !== 1 || !remarkColumns[0].code) return false;
    const remarkKey = remarkColumns[0].code;
    await table.reloadTable();
    // OpenForm 默认加载当月明细。仅操作这张新表单中的行，不影响已有工单。
    if (table.list_data_all.length) {
        table.selectData = [...table.list_data_all];
        table.del();
        await table.reloadTable();
        if (table.list_data_all.length) return false;
    }
    form_data.info.year = year;
    form_data.info.month = month;
    if (!vmAddTableRow("abnormalattendapp", "tabledetail", form_data.setfile.DOCID,
                       JSON.stringify(records))) return false;
    await table.reloadTable();
    if (table.list_data_all.length !== records.length) return false;
    const same = (a, b) => ["rq", "dk", "dk2", "lx", "qjxs", "qjt"].every(
        key => String(a[key] ?? "") === String(b[key] ?? ""));
    const used = new Set();
    for (let i = 0; i < records.length; i++) {
        const matches = table.list_data_all.filter(row => same(row, records[i]) && !used.has(row.rowid));
        if (matches.length !== 1 || !matches[0].rowid) return false;
        const row = matches[0];
        used.add(row.rowid);
        const item = corrections[i];
        const options = choices.filter(option => option.type === item.offset_type);
        if (options.length !== 1) return false;
        table.selectData = [row];
        table.change();
        await Vue.nextTick();
        const modal = window.tableopenvw;
        if (!modal?.listdata || !modal.$el?.isConnected) return false;
        // 和 dianji 打开选择器时使用相同的上下文，执行原抵消类别回调。
        window.listTitle = modal.listdata;
        window.titledata = modal.titledata;
        if (ltseleter_custom_callback2([options[0]]) !== true) return false;
        modal.$set(modal.listdata, remarkKey, item.remark);
        if (item.offset_type === "忘打卡" || item.offset_type === "卡机故障") {
            modal.$set(modal.listdata, "ks", item.actual_start);
            modal.$set(modal.listdata, "js", item.actual_end);
        }
        if (item.offset_type === "卡机故障") modal.$set(modal.listdata, "gzkjwz", item.device_location);
        await Vue.nextTick();
        modal.confirm(); // 保留原必填检查、change 回调和明细保存逻辑。
        if (modal.$el.isConnected) return false;
        await table.reloadTable();
        const saved = table.list_data_all.find(value => value.rowid === row.rowid);
        if (!saved || saved.dxlb !== item.offset_type || saved.dxlbbm !== options[0].typecode ||
            (saved[remarkKey] || "") !== item.remark) return false;
        if (item.actual_start && (saved.ks !== item.actual_start || saved.js !== item.actual_end)) return false;
        if (item.device_location && saved.gzkjwz !== item.device_location) return false;
    }
    // getNodeAction 会执行 beforeGetNode、额度/附件/业务线检查和下一节点解析。
    // 不直接构造 serializeData、authCtrl、审批人或 nextFlow。
    return (await FlowCtlData.getNodeAction("TJ")) !== false;
}
