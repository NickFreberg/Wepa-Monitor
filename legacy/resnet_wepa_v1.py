import datetime
import time
import urllib.request
import winsound
from html.parser import HTMLParser


def current_time():
    current = datetime.datetime.now()
    return str(current.strftime("%a, %b %d, %Y" + "\t%I:%M:%S %p ~"))
    # print(current.strftime("%I:%M:%S %p"))


def area_assignment():

    ugh_printers = ["Shea/Durgin (HC)", "Shea/Durgin (Color)", "Great Hill Apartments",
                    "Tinsley Center", "Weygand (HC)", "Weygand (Color)"]

    park_printers = ["Crimson (HC)", "Crimson (Color)", "DiNardo", "Miles", "Stonehouse"]

    east_printers = ["Shea/Durgin (HC)", "Shea/Durgin (Color)", "Great Hill Apartments",
                    "Tinsley Center", "Weygand (HC)", "Weygand (Color)", "Crimson (HC)", "Crimson (Color)",
                     "DiNardo", "Miles", "Stonehouse"]

    west_printers = ["Scott", "Pope", "Woodward"]

    all_printers = [ugh_printers, park_printers, west_printers]

    area = eval(input("Enter the number of the area you're assigned to:\n"
                      "1. Upper Great Hill\n"
                      "2. University Park\n"
                      "3. East Side (Upper Great Hill AND University Park)\n"
                      "4. West Side\n"
                      "5. Maxwell L9\n"))
    print("\n\n")

    if area == 1:
        # Print all of the kiosks in Upper Great Hill
        print("Printers in your area:")
        for i in range(ugh_printers.__len__()):
            print(ugh_printers[i])
        time.sleep(2)
        return "Upper Great Hill"

    elif area == 2:
        print("Printers in your area:")
        for i in range(park_printers.__len__()):
            print(park_printers[i])
        time.sleep(2)
        return "University Park"

    elif area == 3:
        print("Printers in your area:")
        for i in range(east_printers.__len__()):
            print(east_printers[i])
        time.sleep(2)
        return "East Side"

    elif area == 4:
        print("Printers in your area:")
        for i in range(west_printers.__len__()):
            print(west_printers[i])
        time.sleep(2)
        return "West Side"

    elif area == 5:
        print("All ResNet Printers:"
              "\n_ _ _ _ _ _ _ _ _ _")
        for i in range(all_printers.__len__()):
            if i == 0:
                print("\nUpper Great Hill Area: "
                      "\n______________________")
            elif i == 1:
                print("\nUniversity Park Area: "
                      "\n_____________________")
            elif i == 2:
                print("\nWest Side Area: "
                      "\n_______________")
            # For each array of area printers
            for j in range(all_printers[i].__len__()):
                print(all_printers[i][j])
        time.sleep(2)
        return "L9"

    else:
        print("Invalid entry: please try again.")
        area = area_assignment()


class HTMLTableParser(HTMLParser):
    """ This class serves as a html table parser. It is able to parse multiple
    tables which you feed in. You can access the result per .tables field.
    """
    def __init__(
        self,
        decode_html_entities=False,
        data_separator=' ',
    ):

        HTMLParser.__init__(self)

        self._parse_html_entities = decode_html_entities
        self._data_separator = data_separator

        self._in_td = False
        self._in_th = False
        self._current_table = []
        self._current_row = []
        self._current_cell = []
        self.tables = []

    def handle_starttag(self, tag, attrs):
        """ We need to remember the opening point for the content of interest.
        The other tags (<table>, <tr>) are only handled at the closing point.
        """
        if tag == 'td':
            self._in_td = True
        if tag == 'th':
            self._in_th = True

    def handle_data(self, data):
        """ This is where we save content to a cell """
        if self._in_td or self._in_th:
            self._current_cell.append(data.strip())

    def handle_charref(self, name):
        """ Handle HTML encoded characters """

        if self._parse_html_entities:
            self.handle_data(self.unescape('&#{};'.format(name)))

    def handle_endtag(self, tag):
        """ Here we exit the tags. If the closing tag is </tr>, we know that we
        can save our currently parsed cells to the current table as a row and
        prepare for a new row. If the closing tag is </table>, we save the
        current table and prepare for a new one.
        """
        if tag == 'td':
            self._in_td = False
        elif tag == 'th':
            self._in_th = False

        if tag in ['td', 'th']:
            final_cell = self._data_separator.join(self._current_cell).strip()
            self._current_row.append(final_cell)
            self._current_cell = []
        elif tag == 'tr':
            self._current_table.append(self._current_row)
            self._current_row = []
        elif tag == 'table':
            self.tables.append(self._current_table)
            self._current_table = []


class Kiosk:

    def __init__(self, print_station_number,desc,status_message, printer_text, toner_percentages, drum_percentages, belt_life, fuser_life):
        self.print_station_number = print_station_number
        self.desc = desc
        self.status_message = status_message
        self.printer_text = printer_text
        self.toner_percentages = toner_percentages
        self.drum_percentages = drum_percentages
        self.belt_life = belt_life
        self.fuser_life = fuser_life

def main():
    # Number of print stations -> Will be used for arrays later in the program
    number_of_printers = 14
    kiosks = []
    ugh_kiosks = []
    up_kiosks = []
    west_kiosks = []
    # init_percentages = [100, 100, 100, 100]
    area_rsr = input("Enter your name: ")
    area = area_assignment()

    if area == "L9":
        print("_____________________________________________________________________________________________")
        print("\nHello " + str(area_rsr) + "! Welcome to your shift!"
                                           "\nPlease make sure you open the following:"
                                           "\n\t~ TeamDynamix"
                                           "\n\t~ Zendesk Chat"
                                           "\n\t~ Google Voice"
                                           "\n\t~ ResNet Website"
                                           "\n\t~ ResNet Email"
                                           "\n\t~ OneDrive"
                                           "\nIf you have the closing shift, please forward the office phone at the end of the night."
                                           "\nHave a good shift! :)"
                                           "\n\n\nLoading printer status...")
    else:
        print("_____________________________________________________________________________________________")
        print("\nHello " + str(area_rsr) + "! Welcome to your shift!"
                                           "\nPlease make sure you open the following:"
                                           "\n\t~ TeamDynamix"
                                           "\n\t~ Google Voice"
                                           "\n\t~ ResNet Website"
                                           "\n\t~ ResNet Email"
                                           "\n\t~ OneDrive"
                                           "\nHave a good shift! :)"
                                           "\n\n\nLoading printer status...")


    time.sleep(5)
    iterator = 2
    target = "http://cs.wepanow.com/000BRIDGEW149.html&filter="
    start(area_rsr, area, iterator, target)
    return


def start(area_rsr, area,iterator,target):

    number_of_printers = 14
    kiosks = []
    ugh_kiosks = []
    up_kiosks = []
    west_kiosks = []
    east_kiosks = []

    # Manually put in P.S.# so that we can search for each kiosk in the HTML
    print_station_ids = ["00800", "01270", "00472", "00518", "00516", "00817", "00412",
                         "01102", "00498", "00517", "02061", "00762", "01143", "00477"]

    # Manually enter the Descriptions so that we can search for them in the HTML
    print_station_names = ["Crimson Hall - Lobby", "Crimson Hall - Lobby (HC)", "DiNardo Hall - 1st Floor Lounge",
                           "Great Hill Apartments - Lobby", "Miles Hall - 1st Floor Lounge", "Pope Hall - Lobby",
                           "Scott Hall - Lobby", "Shea/Durgin - 1st Floor Lounge", "Shea/Durgin - 1st Floor Lounge (HC)",
                           "Stonehouse-Lobby", "Tinsley Center", "Weygand Hall - Lobby",
                           "Weygand Hall, Lobby (next to Prod 762) (HC)",
                           "Woodward Hall - 1st Floor Lounge"]
    # get website content
    req = urllib.request.Request(url=target)
    f = urllib.request.urlopen(req)
    xhtml = f.read().decode('utf-8')

    # instantiate the parser and feed it
    p = HTMLTableParser()
    p.feed(xhtml)

    itera = 2

    for i in range(0, number_of_printers):
        printid = print_station_ids[i]

        printer_name = print_station_names[i]

        new_kiosk = Kiosk(p.tables[0][itera][1], p.tables[0][itera][2], p.tables[0][itera][3], p.tables[0][itera][4],
                          [p.tables[0][itera][5], p.tables[0][itera][6], p.tables[0][itera][7], p.tables[0][itera][8]],
                          [p.tables[0][itera][9], p.tables[0][itera][10], p.tables[0][itera][11], p.tables[0][itera][12]],
                          p.tables[0][itera][13], p.tables[0][itera][14])

        kiosks.append(new_kiosk)
        itera += 2

    for i in range(kiosks.__len__()):
        if int(kiosks[i].print_station_number) == 518 or int(kiosks[i].print_station_number) == 1102 or int(kiosks[i].print_station_number) == 498 or int(kiosks[i].print_station_number) == 2061 or int(kiosks[i].print_station_number) == 762 or int(kiosks[i].print_station_number) == 1143:
            #== "00518" or "01102" or "00498" or "02061" or "00762" or "01143":
            ugh_kiosks.append(kiosks[i])
            east_kiosks.append(kiosks[i])
        if int(kiosks[i].print_station_number) == 800 or int(kiosks[i].print_station_number) == 1270 or int(kiosks[i].print_station_number) == 472 or int(kiosks[i].print_station_number) == 516 or int(kiosks[i].print_station_number) == 517:
            up_kiosks.append(kiosks[i])
            east_kiosks.append(kiosks[i])
        if int(kiosks[i].print_station_number) == 817 or int(kiosks[i].print_station_number) == 412 or int(kiosks[i].print_station_number) == 477:
            west_kiosks.append(kiosks[i])

    monitor_printers(area, area_rsr, ugh_kiosks, up_kiosks, west_kiosks, east_kiosks, kiosks)
    time.sleep(60)
    start(area_rsr, area,iterator,target)


def monitor_printers(area, area_rsr, ugh_kiosks,up_kiosks,west_kiosks,east_kiosks,kiosks):
    errors = []
    print("\n\n")
    print("_____________________________________________________________________________________________")
    print("ResNet Wepa Print Station Status")
    print("Area: " + str(area))
    print("RSR: " + area_rsr)
    # # # # # # # # # # # # # # #
    time = current_time()
    print(time)
    # # # # # # # # # # # # # # #
    print("\nWEPA KIOSKS IN YOUR AREA THAT NEED ATTENTION:")
    if area == "Upper Great Hill":
        for i in range(ugh_kiosks.__len__()):

            message_check(ugh_kiosks[i], errors, time)
            toner_check(ugh_kiosks[i], errors, time)
            drum_check(ugh_kiosks[i], errors, time)
            belt_check(ugh_kiosks[i], errors, time)
            fuser_check(ugh_kiosks[i], errors, time)
        return errors

    if area == "University Park":
        for i in range(up_kiosks.__len__()):
            message_check(up_kiosks[i], errors, time)
            toner_check(up_kiosks[i], errors, time)
            drum_check(up_kiosks[i], errors, time)
            belt_check(up_kiosks[i], errors, time)
            fuser_check(up_kiosks[i], errors, time)
        return errors

    if area == "West Side":
        for i in range(west_kiosks.__len__()):
            message_check(west_kiosks[i], errors, time)
            toner_check(west_kiosks[i], errors, time)
            drum_check(west_kiosks[i], errors, time)
            belt_check(west_kiosks[i], errors, time)
            fuser_check(west_kiosks[i], errors, time)
        return errors

    if area == "East Side":
        for i in range(east_kiosks.__len__()):
            message_check(east_kiosks[i], errors, time)
            toner_check(east_kiosks[i], errors, time)
            drum_check(east_kiosks[i], errors, time)
            belt_check(east_kiosks[i], errors, time)
            fuser_check(east_kiosks[i], errors, time)
        return errors

    else:
        for i in range(kiosks.__len__()):
            message_check(kiosks[i], errors, time)
            toner_check(kiosks[i], errors, time)
            drum_check(kiosks[i], errors, time)
            belt_check(kiosks[i], errors, time)
            fuser_check(kiosks[i], errors, time)
        return errors


def message_check(kiosk, errors, time):
    if kiosk.status_message != "":
        print(kiosk.desc,"-->\t", kiosk.status_message)
        err = time + " --> " + kiosk.desc + " --> " + kiosk.status_message
        check = err.split("~"); check.remove(check[0])
        for s in errors:
            s_split = s.split("~"); s_split.remove(s_split[0])
            if s != check:
                errors.append(err)
        winsound.MessageBeep(1)
    if kiosk.printer_text != "":
        print(kiosk.desc,"-->\t", kiosk.printer_text)
        err = time + " --> " + kiosk.desc + " --> " + kiosk.status_message
        check = err.split("~"); check.remove(check[0])
        for s in errors:
            s_split = s.split("~"); s_split.remove(s_split[0])
            if s != check:
                errors.append(err)
        winsound.MessageBeep(1)
    return


def toner_check(kiosk, errors, time):

    black = int(kiosk.toner_percentages[0])
    cyan = int(kiosk.toner_percentages[1])
    magenta = int(kiosk.toner_percentages[2])
    yellow = int(kiosk.toner_percentages[3])

    if black <= 5:
        winsound.MessageBeep(1)
        if black <3:
            print(kiosk.desc,"-->\t WARNING: BLACK TONER CRITICAL. REPLACE AS SOON AS POSSIBLE!")
            err = time + " --> " + kiosk.desc + " --> WARNING: BLACK TONER CRITICAL. REPLACE AS SOON AS POSSIBLE!"
            check = err.split("~")
            check.remove(check[0])
            for s in errors:
                s_split = s.split("~")
                s_split.remove(s_split[0])
                if s != check:
                    errors.append(err)

        else:
            print(kiosk.desc,"-->\tBlack Toner is at",black,"% and needs replacement.")
            err = time + " --> " + kiosk.desc + " --> Black Toner is at",black,"% and needs replacement."
            check = err.split("~") # WORKING ON ERROR LOGGING PROCESS
            check.remove(check[0])
            for s in errors:
                s_split = s.split("~")
                s_split.remove(s_split[0])
                if s != check:
                    errors.append(err)

    if cyan <= 5:
        winsound.MessageBeep(1)
        if cyan <3:
            print(kiosk.desc, "-->\t WARNING: CYAN TONER CRITICAL. REPLACE AS SOON AS POSSIBLE!")
            err = time + " --> " + kiosk.desc + " --> WARNING: CYAN TONER CRITICAL. REPLACE AS SOON AS POSSIBLE!"
            check = err.split("~")
            check.remove(check[0])
            for s in errors:
                s_split = s.split("~")
                s_split.remove(s_split[0])
                if s != check:
                    errors.append(err)
        else:
            print(kiosk.desc,"-->\tCyan Toner is at", cyan, "% and needs replacement.")
            err = time + " --> " + kiosk.desc + " --> " + kiosk.status_message
            check = err.split("~")
            check.remove(check[0])
            for s in errors:
                s_split = s.split("~")
                s_split.remove(s_split[0])
                if s != check:
                    errors.append(err)

    if magenta <= 5:
        winsound.MessageBeep(1)
        if magenta <3:
            print(kiosk.desc, "-->\t WARNING: MAGENTA TONER CRITICAL. REPLACE AS SOON AS POSSIBLE!")
        else:
            print(kiosk.desc,"-->\tMagenta Toner is at", magenta, "% and needs replacement.")

    if yellow <= 5:
        winsound.MessageBeep(1)
        if yellow < 3:
            print(kiosk.desc, "-->\t WARNING: YELLOW TONER CRITICAL. REPLACE AS SOON AS POSSIBLE!")
        else:
            print(kiosk.desc, "-->\tYellow Toner is at", yellow, "% and needs replacement.")
    return


def drum_check(kiosk, errors, time):
    black = int(kiosk.drum_percentages[0])
    cyan = int(kiosk.drum_percentages[1])
    magenta = int(kiosk.drum_percentages[2])
    yellow = int(kiosk.drum_percentages[3])

    if black <= 5:
        winsound.MessageBeep(1)
        if black < 3:
            print(kiosk.desc, "-->\t WARNING: BLACK DRUM CRITICAL. REPLACE AS SOON AS POSSIBLE!")
        else:
            print(kiosk.desc, "-->\tBlack Drum is at",black,"% and needs replacement.")

    if cyan <= 5:
        winsound.MessageBeep(1)
        if cyan < 3:
            print(kiosk.desc, "-->\t WARNING: CYAN DRUM CRITICAL. REPLACE AS SOON AS POSSIBLE!")
        else:
            print(kiosk.desc, "-->\tCyan Drum is at", cyan, "% and needs replacement.")

    if magenta <= 5:
        winsound.MessageBeep(1)
        if magenta < 3:
            print(kiosk.desc, "-->\t WARNING: MAGENTA DRUM CRITICAL. REPLACE AS SOON AS POSSIBLE!")
        else:
            print(kiosk.desc, "-->\tMagenta Drum is at", magenta, "% and needs replacement.")

    if yellow <= 5:
        winsound.MessageBeep(1)
        if yellow < 3:
            print(kiosk.desc, "-->\t WARNING: YELLOW DRUM CRITICAL. REPLACE AS SOON AS POSSIBLE!")
        else:
            print(kiosk.desc, "-->\tYellow Drum is at", yellow, "% and needs replacement.")
    return


def belt_check(kiosk, errors, time):
    if int(kiosk.belt_life) <= 5:
        winsound.MessageBeep(1)
        print(kiosk.desc,"-->\t Belt life is at ", int(kiosk.belt_life),"%."
                                                                        "\nBelts should be changed at 2% on Weekdays and 5% on Fridays")
        if int(kiosk.belt_life) < 2:
            print(kiosk.desc,"-->\t BELT LIFE CRITICAL: REPLACE AS SOON AS POSSIBLE!")
    return


def fuser_check(kiosk, errors, time):
    if int(kiosk.fuser_life) <= 5:
        winsound.MessageBeep(1)
        print(kiosk.desc,"-->\t Fuser life is at ", int(kiosk.fuser_life),"%. Keep an eye on it.")
    return


main()




