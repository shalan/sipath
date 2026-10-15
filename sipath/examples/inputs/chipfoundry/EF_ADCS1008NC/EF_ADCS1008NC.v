/*
	Copyright 2023 Efabless Corp.

	Author: Mohamed Shalan (mshalan@efabless.com)
	
	Licensed under the Apache License, Version 2.0 (the "License"); 
	you may not use this file except in compliance with the License. 
	You may obtain a copy of the License at:
	http://www.apache.org/licenses/LICENSE-2.0
	Unless required by applicable law or agreed to in writing, software 
	distributed under the License is distributed on an "AS IS" BASIS, 
	WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. 
	See the License for the specific language governing permissions and 
	limitations under the License.
*/
`timescale			1ns/1ns
`default_nettype	none

module EF_ADCS1008NC (
`ifdef USE_POWER_PINS
    input real          VDD,
    input real          VSS,
    input real          DVDD,
    input real          DVSS,
`endif
    input real          VH,
    input real          VL,

    input real          \VIN[0] ,
    input real          \VIN[1] ,
    input real          \VIN[2] ,
    input real          \VIN[3] ,
    input real          \VIN[4] ,
    input real          \VIN[5] ,
    input real          \VIN[6] ,
    input real          \VIN[7] ,

    input wire          HOLD,
    input wire          RST,
    input wire          EN,
    
    input wire [2:0]    B,
    
    input wire [9:0]    DATA,
        
    output wire         CMP

);

    real err = 0.33;

    wire real a_mux =   (B==0) ? \VIN[0] :
                        (B==1) ? \VIN[1] :
                        (B==2) ? \VIN[2] :
                        (B==3) ? \VIN[3] :
                        (B==4) ? \VIN[4] :
                        (B==5) ? \VIN[5] :
                        (B==6) ? \VIN[6] : \VIN[7] ;

    real held_value;
    always @(posedge HOLD)
        if(EN)
            held_value = (a_mux + err);
        else
            held_value = 0.0;

    wire real dac_out;

    always @(negedge RST)
        err = 0.0;

    assign dac_out = DATA * (VH-VL)/1024.0;

    assign CMP = (held_value > dac_out) ? 1 : 0;

endmodule