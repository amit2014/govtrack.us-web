"""
Parser of:
 * bill terms located in data/us/[liv, liv111, crsnet].xml
 * bills located in data/us/*/bills/*.xml
"""
from lxml import etree
import logging
from django.db.utils import IntegrityError
import glob
import re
import time
import urllib
import os.path
from datetime import datetime, timedelta

from parser.progress import Progress
from parser.processor import Processor
from parser.models import File
from bill.models import BillTerm, TermType, BillType, Bill, Cosponsor, BillStatus, RelatedBill
from person.models import Person
from bill.title import get_primary_bill_title
from committee.models import Committee
from settings import CURRENT_CONGRESS

log = logging.getLogger('parser.bill_parser')
PERSON_CACHE = {}
TERM_CACHE = {}

def get_person(pk):
    global PERSON_CACHE
    pk = int(pk)
    if not PERSON_CACHE:
        PERSON_CACHE = dict((x.pk, x) for x in Person.objects.all())
    return PERSON_CACHE[pk]


def normalize_name(name):
    "Convert name to common format."

    name = re.sub(r'\s{2,}', ' ', name)
    return name.lower()


def get_term(name, congress):
    global TERM_CACHE
    if not TERM_CACHE:
        for term in BillTerm.objects.all():
            TERM_CACHE[(term.term_type, normalize_name(term.name))] = term
    return TERM_CACHE[(TermType.new if congress >= 111 else TermType.old, normalize_name(name))]

class TermProcessor(Processor):
    REQUIRED_ATTRIBUTES = ['value']
    ATTRIBUTES = ['value']
    FIELD_MAPPING = {'value': 'name'}
    

class BillProcessor(Processor):
    REQUIRED_ATTRIBUTES = ['type', 'session', 'number']
    ATTRIBUTES = ['type', 'session', 'number']
    FIELD_MAPPING = {'type': 'bill_type', 'session': 'congress'}

    def type_handler(self, value):
        return BillType.by_xml_code(value)

    def process(self, obj, node):
        obj = super(BillProcessor, self).process(obj, node)
        self.process_titles(obj, node)
        self.process_introduced(obj, node)
        self.process_sponsor(obj, node)
        self.process_current_status(obj, node)

        # update existing bill record if one exists, otherwise create a new one on save()
        try:
            obj.id = Bill.objects.get(congress=obj.congress, bill_type=obj.bill_type, number=obj.number).id
        except Bill.DoesNotExist:
            pass
            
        obj.save() # save before using m2m relations
        self.process_committees(obj, node)
        self.process_terms(obj, node, obj.congress)
        self.process_consponsors(obj, node)
        self.process_relatedbills(obj, node)
        return obj

    def process_introduced(self, obj, node):
        elem = node.xpath('./introduced')[0]
        obj.introduced_date = self.parse_datetime(elem.get('datetime'))

    def process_current_status(self, obj, node):
        elem = node.xpath('./state')[0]
        obj.current_status_date = self.parse_datetime(elem.get('datetime'))
        obj.current_status = BillStatus.by_xml_code(elem.text)

    def process_titles(self, obj, node):
        titles = []
        for elem in node.xpath('./titles/title'):
            text = unicode(elem.text) if elem.text else None
            titles.append((elem.get('type'), elem.get('as'), text))
        obj.titles = titles
        obj.title = get_primary_bill_title(obj, titles)

    def process_sponsor(self, obj, node):
        try:
            obj.sponsor = get_person(node.xpath('./sponsor')[0].get('id'))
            obj.sponsor_role = obj.sponsor.get_role_at_date(obj.introduced_date)
        except IndexError: # no sponsor node
            obj.sponsor = None
        except TypeError: # no id attribute
            obj.sponsor = None

    def process_consponsors(self, obj, node):
        for subnode in node.xpath('./cosponsors/cosponsor'):
            try:
                person = get_person(subnode.get('id'))
            except IndexError:
                pass
            else:
                joined = self.parse_datetime(subnode.get('joined'))

                value = subnode.get('withdrawn')
                withdrawn = self.parse_datetime(value) if value else None
                ob, isnew = Cosponsor.objects.get_or_create(
                    person=person,
                    bill=obj,
                    defaults={
                        "joined": joined,
                        "withdrawn": withdrawn,
                        "role": person.get_role_at_date(joined)
                    })
                if ob.joined != joined or ob.withdrawn != withdrawn:
                    ob.joined = joined
                    ob.withdrawn = withdrawn
                    ob.save()

    def session_handler(self, value):
        return int(value)

    def number_handler(self, value):
        return int(value)

    def process_committees(self, obj, node):
        comlist = []
        for subnode in node.xpath('./committees/committee'):
            if subnode.get('code') == "": continue
            try:
                com = Committee.objects.get(code=subnode.get('code'))
            except Committee.DoesNotExist:
                log.error('Could not find committee %s' % subnode.get('code'))
            else:
                comlist.append(com)
        obj.committees = comlist

    def process_terms(self, obj, node, congress):
        termlist = []
        for subnode in node.xpath('./subjects/term'):
            name = subnode.get('name')
            try:
                termlist.append(get_term(name, congress))
            except KeyError:
                log.error('Could not find term [name: %s]' % name)
        obj.terms = termlist

    def process_relatedbills(self, obj, node):
        RelatedBill.objects.filter(bill=obj).delete()
        for subnode in node.xpath('./relatedbills/bill'):
            try:
                related_bill = Bill.objects.get(congress=subnode.get("session"), bill_type=BillType.by_xml_code(subnode.get("type")), number=int(subnode.get("number")))
            except Bill.DoesNotExist:
                continue
            RelatedBill.objects.create(bill=obj, related_bill=related_bill, relation=subnode.get("relation"))
                    


def main(options):
    """
    Process bill terms and bills
    """

    # Terms

    term_processor = TermProcessor()
    terms_parsed = set()
    
    # Cache existing terms. There aren't so many.
    existing_terms = { }
    for term in BillTerm.objects.all():
        existing_terms[(int(term.term_type), term.name)] = term

    log.info('Processing old bill terms')
    TERMS_FILE = 'data/us/liv.xml'
    tree = etree.parse(TERMS_FILE)
    for node in tree.xpath('/liv/top-term'):
        term = term_processor.process(BillTerm(), node)
        term.term_type = TermType.old
        try:
            # No need to update an existing term because there are no other attributes.
            term = existing_terms[(int(term.term_type), term.name)]
            terms_parsed.add(term.id)
        except:
            log.debug("Created %s" % term)
            term.save()
            term.subterms.clear()
            
        for subnode in node.xpath('./term'):
            subterm = term_processor.process(BillTerm(), subnode)
            subterm.term_type = TermType.old
            try:
                # No need to update an existing term because there are no other attributes.
                subterm = existing_terms[(int(subterm.term_type), subterm.name)]
                term.subterms.add(subterm) 
                terms_parsed.add(subterm.id)
            except:
                try:
                    log.debug("Created %s" % subterm)
                    subterm.save()
                    term.subterms.add(subterm)
                    
                    existing_terms[(int(subterm.term_type), subterm.name)] = subterm
                    terms_parsed.add(subterm.id)
                except IntegrityError:
                    log.error('Duplicated term %s' % term_processor.display_node(subnode))

    log.info('Processing new bill terms')
    for FILE in ('data/us/liv111.xml', 'data/us/crsnet.xml'):
        tree = etree.parse(FILE)
        for node in tree.xpath('/liv/top-term'):
            term = term_processor.process(BillTerm(), node)
            term.term_type = TermType.new
            try:
                # No need to update an existing term because there are no other attributes.
                term = existing_terms[(int(term.term_type), term.name)]
                terms_parsed.add(term.id)
            except:
                log.debug("Created %s" % term)
                term.save()
                term.subterms.clear()

            for subnode in node.xpath('./term'):
                subterm = term_processor.process(BillTerm(), subnode)
                subterm.term_type = TermType.new
                try:
                    # No need to update an existing term because there are no other attributes.
                    subterm = existing_terms[(int(subterm.term_type), subterm.name)]
                    terms_parsed.add(subterm.id)
                    term.subterms.add(subterm)
                except:
                    try:
                        log.debug("Created %s" % term)
                        subterm.save()
                        term.subterms.add(subterm)
                        
                        existing_terms[(int(subterm.term_type), subterm.name)] = subterm
                        terms_parsed.add(subterm.id)
                    except IntegrityError:
                        log.error('Duplicated term %s' % term_processor.display_node(subnode))

    for term in existing_terms.values():
        if not term.id in terms_parsed:
            log.debug("Deleted %s" % term)
            term.delete()

    # Bills
    
    bill_index = None
    if not options.disable_indexing:
        from bill.search_indexes import BillIndex
        bill_index = BillIndex()

    if options.congress:
        files = glob.glob('data/us/%s/bills/*.xml' % options.congress)
        log.info('Parsing bills of only congress#%s' % options.congress)
    else:
        files = glob.glob('data/us/*/bills/*.xml')
        
    if options.filter:
        files = [f for f in files if re.match(options.filter, f)]
        
    log.info('Processing bills: %d files' % len(files))
    total = len(files)
    progress = Progress(total=total, name='files', step=100)

    bill_processor = BillProcessor()
    seen_bill_ids = []
    for fname in files:
        progress.tick()
        
        if not File.objects.is_changed(fname) and not options.force:
            m = re.search(r"/(\d+)/bills/([a-z]+)(\d+)\.xml$", fname)

            try:
                b = Bill.objects.get(congress=m.group(1), bill_type=BillType.by_xml_code(m.group(2)), number=m.group(3))
                seen_bill_ids.append(b.id)
                
                # Update the index/events for any bill with recently changed text
                textfile = "data/us/bills.text/%s/%s/%s%s.txt" % (m.group(1), m.group(2), m.group(2), m.group(3))
                if (bill_index and not options.disable_events) and os.path.exists(textfile) and File.objects.is_changed(textfile):
                    bill_index.update_object(b, using="bill") # index the full text
                    b.create_events() # events for new bill text documents
                    File.objects.save_file(textfile)
                    
                continue
            except Bill.DoesNotExist:
                pass # just parse as normal
            
        if options.slow:
            time.sleep(1)
            
        skip_stuff = False
            
        tree = etree.parse(fname)
        for node in tree.xpath('/bill'):
            if not skip_stuff:
                bill = bill_processor.process(Bill(), node)
            else:
                m = re.search(r"/(\d+)/bills/([a-z]+)(\d+)\.xml$", fname)
                bill = Bill.objects.get(congress=m.group(1), bill_type=BillType.by_xml_code(m.group(2)), number=m.group(3))
           
            seen_bill_ids.append(bill.id) # don't delete me later
            
            actions = []
            for axn in tree.xpath("actions/*[@state]"):
                actions.append( (repr(bill_processor.parse_datetime(axn.xpath("string(@datetime)"))), BillStatus.by_xml_code(axn.xpath("string(@state)")), axn.xpath("string(text)")) )
            bill.major_actions = actions
            bill.save()
            if bill_index: bill_index.update_object(bill, using="bill")
            
            if not options.disable_events:
                bill.create_events()

        if not skip_stuff:
            File.objects.save_file(fname)
        
    # delete bill objects that are no longer represented on disk.... this is too dangerous.
    if options.congress and not options.filter and False:
        # this doesn't work because seen_bill_ids is too big for sqlite!
        Bill.objects.filter(congress=options.congress).exclude(id__in = seen_bill_ids).delete()
        
    # Parse docs.house.gov for what might be coming up this week.
    import iso8601
    dhg_html = urllib.urlopen("http://docs.house.gov/").read()
    m = re.search(r"class=\"downloadXML\" href=\"(Download.aspx\?file=.*?)\"", dhg_html)
    if not m:
        log.error('No docs.house.gov download link found at http://docs.house.gov.')
    else:
        def bt_re(bt): return re.escape(bt[1]).replace(r"\.", "\.?\s*")
        try:
            dhg = etree.parse(urllib.urlopen("http://docs.house.gov/floor/" + m.group(1))).getroot()
        except:
            print "http://docs.house.gov/" + m.group(1)
            raise
        # iso8601.parse_date(dhg.get("week-date")+"T00:00:00").date()
        for item in dhg.xpath("category/floor-items/floor-item"):
            billname = item.xpath("legis-num")[0].text
            m = re.match("\s*(?:Concur in the Senate Amendment to |Senate Amendment to )?("
                + "|".join(bt_re(bt) for bt in BillType)
                + ")(\d+)\s*(\[Conference Report\]\s*)?$", billname, re.I)
            if not m:
                log.error('Could not parse legis-num "%s" in docs.house.gov.' % billname)
            else:
                for bt in BillType:
                    if re.match(bt_re(bt) + "$", m.group(1)):
                        try:
                            bill = Bill.objects.get(congress=CURRENT_CONGRESS, bill_type=bt[0], number=m.group(2))
                            bill.docs_house_gov_postdate = iso8601.parse_date(item.get("add-date")).replace(tzinfo=None)
                            bill.save()
                            if bill_index: bill_index.update_object(bill, using="bill")
                            
                            if not options.disable_events:
                                bill.create_events()
                        except Bill.DoesNotExist:
                            log.error('Could not find bill "%s" in docs.house.gov.' % billname)
                        break
                else:
                    log.error('Could not parse legis-num bill type "%s" in docs.house.gov.' % billname)

    # Parse Senate.gov's "Floor Schedule" blurb for coming up tomorrow.
    now = datetime.now()
    sfs = urllib.urlopen("http://www.senate.gov/pagelayout/legislative/d_three_sections_with_teasers/calendars.htm").read()
    try:
        sfs = re.search(r"Floor Schedule([\w\W]*)Previous Meeting", sfs).group(1)
        for congress, bill_type, number in re.findall(r"http://hdl.loc.gov/loc.uscongress/legislation.(\d+)([a-z]+)(\d+)", sfs):
            bill_type = BillType.by_slug(bill_type)
            bill = Bill.objects.get(congress=congress, bill_type=bill_type, number=number)
            if bill.senate_floor_schedule_postdate == None or now - bill.senate_floor_schedule_postdate > timedelta(days=7):
                bill.senate_floor_schedule_postdate = now
                bill.save()
                if bill_index: bill_index.update_object(bill, using="bill")
                if not options.disable_events:
                    bill.create_events()
    except Exception as e:
        log.error('Could not parse Senate Floor Schedule: ' + repr(e))


if __name__ == '__main__':
    main()
