import codecs
import json
import dill
import pickle
import inspect
import logging
import sys
import filecmp
import math
import unittest
import numpy

from create_serials import (serializers, test_functions, func_args,
                            expected_outputs)

def func0(x, y={'a': 3}):
    return math.floor(math.log(x)) * y['a']

def func1(x):
    return numpy.linalg.det(x)

def func2(self):
    return self.product(0).iloc[0]

def func3(arr, low, high, x):
    if high >= low:
        mid = (high + low) // 2
        if arr[mid] == x:
            return mid
        elif arr[mid] > x:
            return func3(arr, low, mid - 1, x)
        else:
            return func3(arr, mid + 1, high, x)
    else:
        return -1

def func4(word_list):
    return sorted(word_list)

def func5(word):
    return word[::-2]

def func6(x):
    if (x > 10):
        raise Exception('input is too large')

# Compare serials of different versions
for i in range(len(serializers)):
    for j in range(len(test_functions)):
        f_name36 = "serials/3.6/func" + str(j) + "_0" + str(i+2) + ".txt"
        f_name37 = "serials/3.7/func" + str(j) + "_0" + str(i+2) + ".txt"
        f_name38 = "serials/3.8/func" + str(j) + "_0" + str(i+2) + ".txt"
        compare3637 = filecmp.cmp(f_name36, f_name37, shallow = False)
        compare3638 = filecmp.cmp(f_name36, f_name38, shallow = False)
        print("3.6/3.7 comparison(serializer:", str(i+1),
            "func:", str(j) + ")", compare3637)
        print("3.6/3.8 comparison(serializer:", str(i+1),
            "func:", str(j) + ")", compare3638)
print("\n")

class testSerializers(unittest.TestCase):

    # Tests deserialization of all functions across different versions using
    # serializer 02
    def test_serializer02(self):
        for j in range(len(test_functions)):
            for i in range(6, 9):
                print("func: " + str(j))
                f_name = "serials/3." + str(i) + "/func" + str(j) + "_02.txt"
                fp = open(f_name, "r")
                deserialized_func = serializers[0].deserialize(fp.read())
                print("After deserialization (serialized on 3."
                 + str(i) + "): ")
                if (j == len(test_functions) - 1):
                    with self.assertRaises(Exception) as context:
                        deserialized_func(15)
                    self.assertTrue('input is too large' in \
                    str(context.exception))
                elif type(func_args[j]) is tuple:
                    print("FN(): ", deserialized_func(*func_args[j]))
                    self.assertEqual(deserialized_func(*func_args[j]),
                                                       expected_outputs[j])
                else:
                    print("FN(): ", deserialized_func(func_args[j]))
                    self.assertEqual(deserialized_func(func_args[j]),
                                                       expected_outputs[j])
                fp.close()


    # Testing for func0, func1, func4 fail for serializers 03/04 since they
    # rely on external modules or are recursive
    """
    def test_serializer03(self):
        for j in range(len(test_functions)):
            for i in range(6, 9):
                print("func: " + str(j))
                f_name = "serials/3." + str(i) + "/func" + str(j) + "_02.txt"
                fp = open(f_name, "r")
                deserialized_func = serializers[2].deserialize(fp.read())
                print("After deserialization (serialized on 3." + str(i) +
                      "): ")
                if (j == len(test_functions) - 1):
                    with self.assertRaises(Exception) as context:
                        deserialized_func(15)
                    self.assertTrue('input is too large' in \
                    str(context.exception))
                elif type(func_args[j]) is tuple:
                    print("FN(): ", deserialized_func(*func_args[j]))
                    self.assertEqual(deserialized_func(*func_args[j]),
                                                       expected_outputs[j])
                else:
                    print("FN(): ", deserialized_func(func_args[j]))
                    self.assertEqual(deserialized_func(func_args[j]),
                                                       expected_outputs[j])
                fp.close()

    def test_serializer04(self):
        for j in range(len(test_functions)):
            for i in range(6, 9):
                print("func: " + str(j))
                f_name = "serials/3." + str(i) + "/func" + str(j) + "_03.txt"
                fp = open(f_name, "r")
                deserialized_func = serializers[3].deserialize(fp.read())
                print("After deserialization (serialized on 3." + str(i) +
                      "): ")
                if (j == len(test_functions) - 1):
                    with self.assertRaises(Exception) as context:
                        deserialized_func(15)
                    self.assertTrue('input is too large' in \
                    str(context.exception))
                elif type(func_args[j]) is tuple:
                    print("FN(): ", deserialized_func(*func_args[j]))
                    self.assertEqual(deserialized_func(*func_args[j]),
                                                       expected_outputs[j])
                else:
                    print("FN(): ", deserialized_func(func_args[j]))
                    self.assertEqual(deserialized_func(func_args[j]),
                                                       expected_outputs[j])
                fp.close()
    """

    # Tests for individual functions on serializers 03/04
    def test_serializer03_func2(self):
        for i in range(6, 9):
            f_name = "serials/3." + str(i) + "/func2" + "_03.txt"
            fp = open(f_name, "r")
            deserialized_func = serializers[1].deserialize(fp.read())
            print("After deserialization (serialized on 3." + str(i) +
                  "): ")
            print("FN(): ", deserialized_func(func_args[2]))
            self.assertEqual(deserialized_func(func_args[2]),
                                               expected_outputs[2])
            fp.close()

    def test_serializer04_func2(self):
        for i in range(6, 9):
            f_name = "serials/3." + str(i) + "/func2" + "_04.txt"
            fp = open(f_name, "r")
            deserialized_func = serializers[2].deserialize(fp.read())
            print("After deserialization (serialized on 3." + str(i) +
                  "): ")
            print("FN(): ", deserialized_func(func_args[2]))
            self.assertEqual(deserialized_func(func_args[2]),
                                           expected_outputs[2])
            fp.close()

    def test_serializer03_func4(self):
        for i in range(6, 9):
            f_name = "serials/3." + str(i) + "/func4" + "_03.txt"
            fp = open(f_name, "r")
            deserialized_func = serializers[1].deserialize(fp.read())
            print("After deserialization (serialized on 3." + str(i) +
                  "): ")
            print("FN(): ", deserialized_func(func_args[4]))
            self.assertEqual(deserialized_func(func_args[4]),
                                               expected_outputs[4])
            fp.close()

    def test_serializer04_func4(self):
        for i in range(6, 9):
            f_name = "serials/3." + str(i) + "/func4" + "_04.txt"
            fp = open(f_name, "r")
            deserialized_func = serializers[2].deserialize(fp.read())
            print("After deserialization (serialized on 3." + str(i) +
                  "): ")
            print("FN(): ", deserialized_func(func_args[4]))
            self.assertEqual(deserialized_func(func_args[4]),
                                           expected_outputs[4])
            fp.close()

    def test_serializer03_func5(self):
        for i in range(6, 9):
            f_name = "serials/3." + str(i) + "/func5" + "_03.txt"
            fp = open(f_name, "r")
            deserialized_func = serializers[1].deserialize(fp.read())
            print("After deserialization (serialized on 3." + str(i) +
                  "): ")
            print("FN(): ", deserialized_func(func_args[5]))
            self.assertEqual(deserialized_func(func_args[5]),
                                               expected_outputs[5])
            fp.close()

    def test_serializer04_func5(self):
        for i in range(6, 9):
            f_name = "serials/3." + str(i) + "/func5" + "_04.txt"
            fp = open(f_name, "r")
            deserialized_func = serializers[2].deserialize(fp.read())
            print("After deserialization (serialized on 3." + str(i) +
                  "): ")
            print("FN(): ", deserialized_func(func_args[5]))
            self.assertEqual(deserialized_func(func_args[5]),
                                           expected_outputs[5])
            fp.close()

    def test_serializer03_func6(self):
        for i in range(6, 9):
            f_name = "serials/3." + str(i) + "/func6" + "_03.txt"
            fp = open(f_name, "r")
            deserialized_func = serializers[1].deserialize(fp.read())
            with self.assertRaises(Exception) as context:
                deserialized_func(15)
            self.assertTrue('input is too large' in \
            str(context.exception))
            fp.close()

    def test_serializer04_func6(self):
        for i in range(6, 9):
            f_name = "serials/3." + str(i) + "/func6" + "_04.txt"
            fp = open(f_name, "r")
            deserialized_func = serializers[2].deserialize(fp.read())
            with self.assertRaises(Exception) as context:
                deserialized_func(15)
            self.assertTrue('input is too large' in \
            str(context.exception))
            fp.close()

if __name__ == '__main__':
    unittest.main()
