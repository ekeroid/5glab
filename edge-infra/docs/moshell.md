# Useful MOShell Commands

1. Load all MOs (go to theBBU): 
    ```bash
    lt all 
    ```
    NOTE:  `lt` = load tree  `.` = everything 

2. Show state of a module: 
    ```bash
    st = state 
    ```
3. Get information of a MO or function: 
    ```
    get 
    ```
    eg. To get info about drx 
    ```
    get . drx 
    ```
4. Lock cell (all) 
    ```
    bl cell 
    ```
5. De-block cell (all) 
    ```
    deb cell 
    ```
6. Remove DRx
   ```    
   set GNBDUFunction=1,UeCC=1,DrxProfile=Default,DrxProfileUeCfg=Base drxEnabled false
   ```
   
   set GNBDUFunction=1,UeCC=1,Prescheduling=1,PreschedulingUeCfg=Base preschedulingUeMode 1 
   
   get .*NRSectorCarrier=.* bSChannelBw*                                                                                                                   
                                                                                                                                                          
     260513-14:59:23+0000 169.254.2.2 26.0c MSRBS_NODE_MODEL_25.Q2_702.28391.135_882e_TESTMOM stopfile=/tmp/3112                                             
     =================================================================================================================                                       
     MO                                                      Attribute         Value                                                                         
     =================================================================================================================                                       
     NRSectorCarrier=1_S:1                                   bSChannelBwDL     80                                                                            
     NRSectorCarrier=1_S:1                                   bSChannelBwUL     80                                                                            
     NRSectorCarrier=2_S:2                                   bSChannelBwDL     80                                                                            
     NRSectorCarrier=2_S:2                                   bSChannelBwUL     80                                                                            
     =================================================================================================================                                       
     Total: 2 MOs                                                                                                                                            
   
 
 
Current Settings
                                                                                                       
   ┌───────────────────────┬─────────────────────────────────┬───────────────────────────┐                         
   │       Parameter       │              Value              │          Meaning          │
   ├───────────────────────┼─────────────────────────────────┼───────────────────────────┤                             
   │ subCarrierSpacing     │ 30 kHz                          │ Numerology 1, 0.5ms slots │
   ├───────────────────────┼─────────────────────────────────┼───────────────────────────┤                             
   │ tddUlDlPattern        │ 2 (TDD_ULDL_PATTERN_02)         │ See below                 │                            
   ├───────────────────────┼─────────────────────────────────┼───────────────────────────┤                             
   │ tddSpecialSlotPattern │ 2 (TDD_SPECIAL_SLOT_PATTERN_02) │ Special slot config       │                             
   ├───────────────────────┼─────────────────────────────────┼───────────────────────────┤                             
   │ Band                  │ n78 (ARFCN 650666)              │ 3.5 GHz                   │
   ├───────────────────────┼─────────────────────────────────┼───────────────────────────┤
   │ Bandwidth             │ 80 MHz                          │                           │
   └───────────────────────┴─────────────────────────────────┴───────────────────────────┘
   
  TDD_ULDL_PATTERN_02                                                                                                                                     
  On Ericsson, pattern 02 is typically DDDSU (5ms periodicity):                                                                                           
  - D = Downlink slot (0.5ms)                               
  - S = Special slot (DL + guard + UL symbols)                                                                  
  - U = Uplink slot (0.5ms)                                 

  This means: 3 DL slots → 1 special → 1 UL slot → repeat every 2.5ms

  Latency implications                                                                                                                                    
  With DDDSU at 30kHz:                                                                                                  - DL latency: UE can receive in any D slot → min ~0.5ms, avg ~1ms
  - UL latency: UE must wait for the next U slot → worst case 2.5ms wait + processing
  - Round-trip: ~4-5ms at best (scheduling + HARQ + processing)                      

  Your measured ~10ms includes: TDD wait (~2ms avg) + HARQ feedback (~2ms) + scheduling grant (~2ms) + UE/gNB processing (~2ms) + USB adapter buffering   
  (~2ms).                                                                                                                                                 
------------------------------------------------------------

set NRCellDU=2 administrativeState 0 
set NRCellDU=2 tddUlDlPattern 3 
set NRCellDU=2 tddSpecialSlotPattern 3
set NRCellDU=2 administrativeState 1 

get NRCellDU=2 cellState                                                                                               get NRCellDU=2 operationalState                                                                                        get NRCellDU=2 administrativeState                                                                                                                      
You want to see:                                                                                                        - cellState = 2 (ACTIVE)                                                                                               - operationalState = 1 (ENABLED)                                                                                       - administrativeState = 1 (UNLOCKED) 

set GNBDUFunction=1,UeCC=1,DrxProfile=Default,DrxProfileUeCfg=Base drxEnabled false
set GNBDUFunction=1,UeCC=1,Prescheduling=1,PreschedulingUeCfg=Base preschedulingUeMode 1  

-----------------------------------------------------------

get .* msg3MaxHarqTx

260513-15:47:56+0000 169.254.2.2 26.0c MSRBS_NODE_MODEL_25.Q2_702.28391.135_882e_TESTMOM stopfile=/tmp/3590
=================================================================================================================
MO                                                      Attribute         Value
=================================================================================================================
NRCellDU=1                                              msg3MaxHarqTx     4
NRCellDU=2                                              msg3MaxHarqTx     4
=================================================================================================================
Total: 2 MOs


-----------------------------------------------------------

get NRCellDU=2 configuredGrantPeriodicity                                                                                                               
260513-15:49:58+0000 169.254.2.2 26.0c MSRBS_NODE_MODEL_25.Q2_702.28391.135_882e_TESTMOM stopfile=/tmp/3590                                             
=================================================================================================================      
MO                                                      Attribute         Value                                        
================================================================================================================
=      
NRCellDU=2                                              configuredGrantPeriodicity 10                                  
=================================================================================================================      
Total: 1 MOs                                                                                                                                            


```
get Router
```

Kolla alarm

al