"""order utilities for the Cascadia project"""
import os, logging, datetime
import pandas as pd
from .common import USPS_EXPORT_COLS, use_best_address

# Set up logging
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
logging.basicConfig()
LOG = logging.getLogger(__name__)
LOG.setLevel(LOG_LEVEL)


def assign_cascadia_location(orders):
    '''Assign orders to the desired Cascadia sublocation'''
    LOG.debug(f'Assigning Cascadia sublocations to each order record.')
    orders['Project Name'] = orders[['Project Name']].apply(
        lambda x: 'CASCADIA_SEA' if x['Project Name'] == 2 else 'CASCADIA_PDX', axis=1
    )

    # ensure that we convert column type from int to str
    orders['Project Name'] = orders['Project Name'].astype(str)

    return orders


def filter_cascadia_orders(orders):
    '''Filters Cascadia `orders` to those that we need to create orders for'''
    LOG.debug(f'Filtering <{len(orders)}> Cascadia orders.')

    # enrollment records are non symptom survey records
    enrollment_records = orders[
        orders['redcap_repeat_instrument'] != 'symptom_survey'
    ].copy()

    # apply the project name mapping to each enrollment record. by default
    # it only appears on the first record.
    enrollment_records['Project Name'] = enrollment_records.apply(
        lambda x: enrollment_records.filter(
            items=[(x.name[0], '0_arm_1')], axis=0
        )['Project Name'].values[0],axis=1
    )

    # orders we must fulfill are symptom surveys without an existing tracking number
    # which have a designated pickup time and have a swab trigger. We can drop records
    # which do not have a order date. We only need to schedule max one pickup per
    # participant so can simply keep the final index entry associated with them. Finally
    # we should also apply the best address for each remaining row of the order sheet.
    orders = orders[
        (orders['redcap_repeat_instrument'] == 'symptom_survey') &
        (orders['ss_return_tracking'].isna()) &
        any(orders[['Pickup 1', 'Pickup 2']].notna()) &
        (orders['ss_trigger_swab'])
    ].dropna(subset=['Order Date']
    ).query("~index.duplicated(keep='last')"
    ).apply(lambda record: use_best_address(enrollment_records, record), axis=1)

    # Set today tomorrow variable based on pickup time preference
    orders['Today Tomorrow'] = orders[['Pickup 1']].apply(lambda x: 0 if x['Pickup 1'] == 1 else 1, axis=1)
    orders['Notification Pref'] = 'email'

    orders = assign_cascadia_location(orders)

    return orders

def append_order(orders, household, sku, quantity, address):
    """
    Append household orders to the broader order form
    """
    # don't append orders lacking a valid address
    if any(pd.isna(address['Street Address'])) and any(pd.isna(address['City'])) and any(pd.isna(address['State'])):
        LOG.warning(f'No valid address for household <{household}>. Skipping order.')
        return orders

    if quantity > 20 and sku == 1:  # seperate replenishment kits into other order becaues of max shippment size
        LOG.debug(f'Splitting resupply order for household <{household}> because needed kits > 20.')
        orders = append_order(orders, household, sku, quantity - 20, address)
        quantity = 20
    elif quantity > 4 and sku == 3:  # seperate welcome kits into other order because of max shippment size
        LOG.debug(f'Splitting welcome order for household <{household}> because welcome kits > 4.')
        orders = append_order(orders, household, sku, quantity - 4, address)
        quantity = 4

    address['SKU'] = sku
    address['Quantity'] = quantity
    address['OrderID'] = generate_order_number(address, orders)
    address['Household ID'] = household

    LOG.info(f'Appending order with <{quantity}> kits of type <{sku}> destined for household <{household}>.')
    return pd.concat([orders, address], join='inner', ignore_index=True)


def get_household_address(household_records, house_id):
    """Get the most up to date address from a household"""
    enroll_address = get_enrollment_address(household_records.loc[house_id], house_id)
    updated_address = get_most_recent_address(household_records.loc[house_id], house_id)

    # use the more recent symptom survey address if one exists
    address = enroll_address if updated_address is None else updated_address

    # always use original delivery instructions since symptom survey has no
    # additional dropoff instructions
    address['Pref First Name']       = get_best_first_name(enroll_address)
    address['Last Name']             = enroll_address['Last Name']
    address['Email']                 = enroll_address['Email']
    address['Phone']                 = enroll_address['Phone']
    address['Delivery Instructions'] = enroll_address['Delivery Instructions']

    LOG.debug(f'Setting address region and zipcode.')
    address['Project Name'] = 'Cascadia_SEA' if address['Project Name'].values == 2 else 'Cascadia_PDX'
    address['Zipcode'] = address['Zipcode'].astype(int) if not pd.isna(address['Zipcode'].values) else ''

    return address[address.columns.intersection(USPS_EXPORT_COLS)]


def get_most_recent_address(household_records, house_id):
    """Get the most recent address provided by a household"""
    LOG.debug(f'Trying to select the most recent symptom survey address within household <{house_id}>.')

    # get a households symptom surveys, which may hold additional addresses
    symptom_surveys = household_records[household_records['redcap_repeat_instrument'] == 'symptom_survey'].copy()
    symptom_surveys['ss_date_1'] = symptom_surveys['ss_date_1'].astype('datetime64')

    # sort symptom survey by most recently completed
    # note: we assume non-empty values for `Street Address 2`, `City 2`, and `State 2`
    # implies a 'complete' address. The appended `2` indicates the value is from the
    # symptom survey and not the enrollment survey
    symptom_surveys = symptom_surveys.sort_values(by='ss_date_1', ascending = False)
    complete_addresses = symptom_surveys[
                            ~(
                                (pd.isna(symptom_surveys['Street Address 2'])) &
                                (pd.isna(symptom_surveys['City 2'])) &
                                (pd.isna(symptom_surveys['State 2']))
                            )
                        ].copy()

    if not complete_addresses.empty:
        LOG.debug(f'Address found within participant symptom surveys, updating participant address.')
        complete_addresses['Street Address'] = complete_addresses['Street Address 2']
        complete_addresses['Apt Number'] = complete_addresses['Apt Number 2']
        complete_addresses['City'] = complete_addresses['City 2']
        complete_addresses['State'] = complete_addresses['State 2']
        complete_addresses['Zipcode'] = complete_addresses['Zipcode 2']

        return complete_addresses.iloc[[0]]
    else:
        LOG.debug(f'No symptom survey address found, using Head of Household enrollment address.')
        return None


def get_enrollment_address(household_records, house_id):
    """
    Get the address from the head of household in a passed household's
    enrollment event.
    """
    head_of_house_idx = get_head_of_household(household_records, house_id)

    LOG.debug(f'Fetching Head of Household enrollment address at index <{head_of_house_idx}> for household <{house_id}>.')
    return household_records.loc[[f'{head_of_house_idx}_arm_1']].query('redcap_repeat_instrument.isna()')


def generate_order_number(address, orders):
    order_id = f'{datetime.datetime.now().strftime("%y%m%d")}{address.index[0][0]}'
    while order_id in orders['OrderID'].values:
        if (not order_id[len(order_id) - 1].isalpha()):
            order_id = order_id + 'a'
        else:
            l = list(order_id)
            l[len(l) - 1] = chr(ord(l[len(l) - 1]) + 1)
            order_id = ''.join(l)

    LOG.debug(f'Generated unique order_id <{order_id}>.')
    return order_id


def get_best_first_name(enroll_address):
    '''
    Return the preferred first name of the participant if it exists or their
    full first name if it does not.
    '''
    pref_first_name = enroll_address.iloc[0]['Pref First Name']
    if not pd.isna(pref_first_name):
        LOG.debug(f'Using participant preferred first name.')
        return pref_first_name
    else:
        LOG.debug(f'Using participant legal first name.')
        return enroll_address.iloc[0]['First Name']


def get_head_of_household(household_records, house_id):
    """Gets the head of household index for a given household"""

    # get the head of house id, which is the first non NaN value in this household's data set.
    # We have to reset the index to avoid a duplicated index error and then grab the index of the
    # actual head of house record based on the head of house id
    tmp = household_records.copy()
    tmp.reset_index(level=0, inplace=True)
    head_of_house_idx = tmp.iloc[tmp["HH Reporter"].notna().idxmax()]['HH Reporter']

    # Fallback on the first participant in a household if there is no head of household set
    if pd.isna(head_of_house_idx):
        head_of_house_idx = 0
        LOG.warning(f"No Head of Household detected for household <{house_id}>, falling back to index <{head_of_house_idx}>.")
    else:
        LOG.debug(f"Found index <{int(head_of_house_idx)}> to be the head of household for household <{house_id}>.")

    return int(head_of_house_idx)