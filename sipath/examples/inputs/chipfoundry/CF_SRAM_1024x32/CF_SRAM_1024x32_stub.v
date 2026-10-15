// SPDX-FileCopyrightText: 2025 Umbralogic Technologies LLC d/b/a ChipFoundry and its Licensors, All Rights Reserved
// ========================================================================================
//
// This software is proprietary and protected by copyright and other intellectual property
// rights. Any reproduction, modification, translation, compilation, or representation
// beyond expressly permitted use is strictly prohibited.
//
// Access and use of this software are granted solely for integration into semiconductor
// chip designs created by you as part of ChipFoundry shuttles or ChipFoundry managed
// production programs. It is exclusively for Umbralogic Technologies LLC d/b/a ChipFoundry production purposes, and you may
// not modify or convey the software for any other purpose.
//
// DISCLAIMER: UMBRALOGIC TECHNOLOGIES LLC D/B/A CHIPFOUNDRY AND ITS LICENSORS PROVIDE THIS MATERIAL "AS IS," WITHOUT
// WARRANTIES OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING, BUT NOT LIMITED TO, THE
// IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE.
// Umbralogic Technologies LLC d/b/a ChipFoundry reserves the right to make changes without notice. Neither Umbralogic Technologies LLC d/b/a ChipFoundry nor its
// licensors assume any liability arising from the application or use of any product or
// circuit described herein. Umbralogic Technologies LLC d/b/a ChipFoundry products are not authorized for use as components
// in life-support devices.
//
// This license is subject to the terms of any separate agreement you have with Umbralogic Technologies LLC d/b/a ChipFoundry
// concerning the use of this software, which shall control in case of conflict.

module CF_SRAM_1024x32_stub (DO, ScanOutCC, AD, BEN, CLKin, DI, EN, R_WB, ScanInCC, ScanInDL, ScanInDR, SM, TM, WLBI, WLOFF, vgnd, vnb, vpb, vpwra, vpwrac, vpwrm, vpwrp, vpwrpc);
    output [31:0] DO;
    output ScanOutCC;
    input [31:0] DI;
    input [31:0] BEN;
    input [9:0] AD;
    input EN;
    input R_WB;
    input CLKin;
    input WLBI;
    input WLOFF;
    input TM;
    input SM;
    input ScanInCC;
    input ScanInDL;
    input ScanInDR;
    input vpwrac;
    input vpwrpc;
    input vgnd;
    input vpwrm;

`ifdef CF_SRAM_PA_SIM
  inout vpwra;
`else
  input vpwra;
`endif


`ifdef CF_SRAM_PA_SIM
  inout vpwrp;
`else
  input vpwrp;
`endif

    input vnb;
    input vpb;
endmodule
