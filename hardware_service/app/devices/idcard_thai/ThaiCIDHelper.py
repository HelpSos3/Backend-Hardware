"""
    Class ThaiCIDHelper  
    ไฟล์ประกอบของการอ่านข้อมูลบัตรประชาชน
    วรเพชร  เรืองพรวิสุทธิ์
    09/01/2567
    Python 3.11.5
    pyscard==2.0.7
    update : 10/01/2567 15:00 น
"""

import time, codecs , subprocess
# pyscard 2.0.7
from smartcard.System import readers
from smartcard.util import toHexString

from .DataThaiCID  import *

####- --------------------------------------------------
# Thai CID Smartcard Helper
####- --------------------------------------------------
class ThaiCIDHelper():
    
    def __init__(self,
                 apduSELECT = [0x00, 0xA4, 0x04, 0x00, 0x08],
                 apduTHCard = [0xA0, 0x00, 0x00, 0x00, 0x54, 0x48, 0x00, 0x01], 
                 showThaiDate=True):
        
        # Initialize
        self.cardReaderList = readers()  # PC/SC
        self.cardReader = None
        self.cardReaderIndex = -1
        self.apduSELECT  = apduSELECT
        self.apduTHCard = apduTHCard
        self.apduRequest = []
        self.ATR = ""
        self.showThaiDate = showThaiDate
        self.lastError = ""
                
        print (f'Reader: Available Count = {len(self.cardReaderList)}')
        
####- --------------------------------------------------    
#### connectReader
####- --------------------------------------------------    
    def connectReader(self,index):
        """
            connectReader ติดต่อเครื่องอ่านบัตร \n
            พารามิเตอร์ : 
            index ลำดับของเครื่องอ่านบัตร \n
            Default 0 (ตัวที่ 1) ** ถ้ามี ** \n
            return >> Reader-connection , Connected Status
        """
        
        print(f'Reader: Select ... [{index}] = [{self.cardReaderList[index]}]')
        _HWcardReader = self.cardReaderList[index]
        _connected = False
        
        try: 
            # Create Connection
            self.cardReader = _HWcardReader.createConnection()
            
            # Reader Connection [OK]
            self.cardReader.connect()                 
            self.cardReaderIndex = index
            
            _connected = True
            print(f'Reader: Connected ... [{self.cardReaderList[index]}]')
        
        except Exception as err:
            self.lastError = f'{err}'
            print(f'Connection : Error = {err}')
            # 0x80100069 Unable to connect: The smart card has been removed,so that further communication is not possible
            # Unable to connect with protocol: T0 or T1. The smart card is not responding to a reset.
        
                        
        if _connected == True:
            ### read ATR (format for storage cards)
            # 3B 8N 80 01 {XX} {XX}
            # 3B 8N 80 01 80 4F 0C A0 00 00 03 06 {SS} {NN} 00 00 00 00 {XX}
            
            atr = self.cardReader.getATR()
            self.ATR = toHexString(atr)
            print (f"Reader: ATR = {self.ATR}")
            
            ### Check Version
            # Get-Data command format
            if (atr[0] == 0x3B & atr[1] == 0x67):
                self.apduRequest = [0x00, 0xc0, 0x00, 0x01]
            else:
                self.apduRequest = [0x00, 0xc0, 0x00, 0x00]        

            return  self.cardReader ,_connected 

        return None,False
    
    
####- --------------------------------------------------    
#### readData
####- --------------------------------------------------    

    def readData(self,readPhoto=True,
                 saveText : SaveType = SaveType.NONE,
                 savePhoto: SaveType = SaveType.NONE):
        """
            readData อ่านข้อมูลจากบัตร ตาม apdu ที่กำหนด \n
            พารามิเตอร์ : \n
            readPhoto อ่านรูปภาพ ? \n
            saveText บันทึกข้อความ ? None-File-Clip \n
            savePhoto บันทึกข้อมูลภาพ ? None-File-Clip \n
        """
        start_time = time.time()
       
        # เริ่มอ่านข้อมูลบัตร 
        # SELECT + CARD TYPE
        data, sw1, sw2 = self.cardReader.transmit(self.apduSELECT + self.apduTHCard)
        print ("Reader: Send `SELECT` Response = %02X %02X" % (sw1, sw2))

        responseJson = []
        _jsonThaiDesc , _Json4Dev ,  _JsonRawData = {},{},{}
        _textThaiDesc , _textJson = "",""

        ##- -----------------------------------------------------
        ### Read Value
        print("Reader: อ่านข้อมูล เริ่มแล้ว...")
        apduCount = len(APDU_DATA) 
        for index,data in enumerate(APDU_DATA):
            
            _apdu = searchDATAValue('key',data['key'],'apdu')
            print('Reader: อ่าน ',data['desc'])
            
            response = self.getValue(_apdu,data['type'])
        ##- ----------------------------------------------------
        
            # make Json
            _jsonThaiDesc[data['desc']] = response[0]
            _Json4Dev[data['id']] = response[0]
            #_JsonRawData['raw'] = response[1]
            
            # make Text
            if index == (apduCount-1): 
                _textThaiDesc += f'"{index}":"{data["desc"]}={response[0]}"\n'
                _textJson += f'"{data["id"]}":"{response[0]}"\n'
            else:
                _textThaiDesc += f'"{index}":"{data["desc"]}={response[0]}",\n'
                _textJson += f'"{data["id"]}":"{response[0]}",\n'
                
        
        ### make Json List
        responseJson.append(_jsonThaiDesc)
        responseJson.append(_Json4Dev)
        # responseJson.append(_JsonRawData)
        
        ### Copy Text To Clipboard
        if saveText == SaveType.CLIPBOARD:
            print("Reader: บันทึกข้อมูลข้อความ [ไปคลิปบอร์ด] ...")
            copyTextToClipboard(f'{_textThaiDesc}\n{_textJson}')
    
    
        if saveText == SaveType.FILE:
            print("Reader: บันทึกข้อมูลข้อความ [ลงไฟล์] ...")
            ### Save Text To File
            filename = f"{responseJson[1]['CID']}.json"
            with open(filename, "wb" ) as f:
                
                f.write ('[{\n'.encode('utf-8'))
                f.write (_textThaiDesc.encode('utf-8'))
                f.write ('},\n'.encode('utf-8'))

                f.write ('{\n'.encode('utf-8'))
                f.write (_textJson.encode('utf-8'))
                f.write ('}]'.encode('utf-8'))

                f.close    

        ##- -----------------------------------------------------
        ### Read Photo
        if readPhoto:
            print("Reader: อ่าน  รูปภาพ...")
            photoStr = []
            for data in APDU_PHOTO:
                _apdu = searchAPDUPhoto(data['key'])
                photoStr += self.getPhoto(_apdu)            
            
            ### Save Photo to File
            if savePhoto == SaveType.FILE:
                # use bytes แปลง [] เป็น Bytes ** ถ้าเป็น String จะ Error
                print("Reader: บันทึกข้อมูลรูป [ลงไฟล์] ...")
                imageData = bytes(photoStr)
                filename = f"{responseJson[1]['CID']}.jpg"
                with codecs.open(filename, "wb" ) as f:
                    f.write (imageData)
                    f.close
                    

        ##- -----------------------------------------------------
        end_time = time.time()
        elapsed_time = end_time - start_time
        elapsed_str  = time.strftime("%S.{}".format(str(elapsed_time % 1)[2:])[:6], time.gmtime(elapsed_time))

        print(f"Reader: อ่านข้อมูล เสร็จแล้ว... [{elapsed_str} ms]")
        

####- --------------------------------------------------    
#### getValue
####- --------------------------------------------------   

    def getValue(self,apdu,dataType):
        """
            getValue อ่านข้อมูลจากบัตร ตาม APDU ที่กำหนด \n
            พารามิเตอร์ : \n
            apdu คำสั่ง APDU ของฟิลด์ที่ต้องการอ่าน \n
            dataType ประเภทของข้อมูล เพื่อจัดรูปแบบตามต้องการ
        """
        
        #print(f"Reader: Send Command")
        _data, _sw1, sw2 = self.cardReader.transmit(apdu)
        #print(f"Reader: Card Response1 >> Size= {sw2} ")
        #ขอข้อมูล ขนาดที่บอกมา Size == sw2
        rawdata, _sw1 , _sw2 = self.cardReader.transmit(self.apduRequest + [sw2])
        #print(f"Reader: Card Response2 >> data")
        
        text = self.encodeTextThai(rawdata)
              
        if dataType == ThaiCIDDataType.ADDRESS:
            # ข้อมูลที่อยู่
            text = text.replace('#######',' ')
            text = text.replace('######',' ')
            text = text.replace('#####',' ')
            text = text.replace('####',' ')
            text = text.replace('###',' ')
            text = text.replace('##',' ')
            text = text.replace('#',' ')
            data = text
            
        elif dataType == ThaiCIDDataType.NAME:
            # ข้อมูลชื่อ-นามสกุล ไทย และ อังกฤษ
            text = text.replace('##',' ')
            text = text.replace('#','')
            data = text
            
        

        else:
            data = text
        
        return [data, rawdata]


    def getPhoto(self,apdu):
        """
            getPhoto อ่านข้อมูลรูปภาพจากบัตร ตาม APDU ที่กำหนด \n
            พารามิเตอร์ : \n
            apdu คำสั่ง APDU ของฟิลด์ที่ต้องการอ่าน \n
        """
                
        _ , _ , sw2 = self.cardReader.transmit(apdu)        
        #ขอข้อมูล ขนาดที่บอกมา Size == sw2        
        rawdata, _ , _ = self.cardReader.transmit(self.apduRequest + [sw2])

        return rawdata


    def encodeTextThai(self,data):

        # แปลงเป็นตัวอักษรไทย TIS-620
        result = bytes(data).decode('tis-620')

        # ตัดช่องว่าง trim
        return result.strip();

## External Method ##
def textToThaiDate(txt:str):
    # ปีพ.ศ. + เดือน + ปี 
    # บัตร คนมีปัญหา มีแต่ปีเกิด
    # บัตร ตลอดชีพ มีแต่ปีเกิด
    if len(txt) == 8:
        _year   = txt[:4]   # 1-4
        _month  = txt[4:6]  # 5-6
        _day    = txt[6:8]  # 7-8
        # แปลงเป็น วัน/เดือน/ปี
        return f"{_day}/{_month}/{_year}"
    else:
        return txt


def textToEngDate(txt:str):
    # ปีพ.ศ. + เดือน + ปี 
    if len(txt) == 8:
        _year   = txt[:4]   # 1-4
        _month  = txt[4:6]  # 5-6
        _day    = txt[6:8]  # 7-8
        _yearEN = int(_year) - 543
        # แปลงเป็น ปี - เดือน - วัน
        return f"{_yearEN}-{_month}-{_day}"
    else:
        return txt
        
    
def setformatDocNumber(txt:str):
    # 0000-00-00000000    
    t1 = txt[:4]   # 1-4
    t2 = txt[4:6]  # 5-6
    t3 = txt[6:]   # 6+
    
    return f"{t1}-{t2}-{t3}"
    
    
def searchDATAValue(type,value,response):    
    for data in APDU_DATA:
        if data[type] == value:
           return data[response]

    return None



def searchAPDUPhoto(value):
    
    for data in APDU_PHOTO:
        if data['key'] == value:
           return data['apdu']

    return None



def copyTextToClipboard(txt):

    return subprocess.run("clip", input=txt, check=True, encoding="tis-620")